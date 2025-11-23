########################################
#            TERRAFORM SETUP
########################################
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.26"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.13"
    }
  }
}

########################################
#               VARIABLES
########################################
variable "region" {
  type    = string
  default = "ap-northeast-1"
}

variable "cluster_name" {
  type    = string
  default = "session-eks"
}

variable "vpc_cidr" {
  type    = string
  default = "10.0.0.0/16"
}

variable "alert_email" {
  type        = string
  description = "Email for EKS pod alert notifications"
}

########################################
#              PROVIDERS
########################################
provider "aws" {
  region = var.region
}

# EKS cluster lookup for kubernetes/helm auth
data "aws_eks_cluster" "this" {
  name = module.eks.cluster_name
}

data "aws_eks_cluster_auth" "this" {
  name = module.eks.cluster_name
}

provider "kubernetes" {
  host                   = data.aws_eks_cluster.this.endpoint
  cluster_ca_certificate = base64decode(data.aws_eks_cluster.this.certificate_authority[0].data)
  token                  = data.aws_eks_cluster_auth.this.token
}

provider "helm" {
  kubernetes {
    host                   = data.aws_eks_cluster.this.endpoint
    cluster_ca_certificate = base64decode(data.aws_eks_cluster.this.certificate_authority[0].data)
    token                  = data.aws_eks_cluster_auth.this.token
  }
}

########################################
#                 VPC
########################################
module "vpc" {
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "${var.cluster_name}-vpc"
  cidr = var.vpc_cidr

  azs             = ["${var.region}a", "${var.region}c"]
  private_subnets = ["10.0.1.0/24", "10.0.2.0/24"]
  public_subnets  = ["10.0.101.0/24", "10.0.102.0/24"]

  enable_nat_gateway = true
  single_nat_gateway = true

  public_subnet_tags = {
    "kubernetes.io/role/elb"                    = "1"
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
  }

  private_subnet_tags = {
    "kubernetes.io/role/internal-elb"           = "1"
    "kubernetes.io/cluster/${var.cluster_name}" = "shared"
  }
}

########################################
#                 EKS
########################################
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = var.cluster_name
  cluster_version = "1.30"

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  enable_irsa = true

  eks_managed_node_groups = {
    default = {
      instance_types = ["t3.medium"]
      desired_size   = 2
      min_size       = 1
      max_size       = 4
    }
  }
}

########################################
#    CLOUDWATCH OBSERVABILITY (IRSA)
########################################
resource "aws_iam_role" "eks_cw_agent_role" {
  name = "EKSCloudWatchObservabilityRole"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Principal = {
          Federated = module.eks.oidc_provider_arn
        }
        Action = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "${module.eks.oidc_provider}:aud" = "sts.amazonaws.com"
          }
        }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "cw_permissions" {
  role       = aws_iam_role.eks_cw_agent_role.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy"
}

resource "aws_iam_role_policy_attachment" "cw_cni" {
  role       = aws_iam_role.eks_cw_agent_role.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonEKS_CNI_Policy"
}

resource "aws_iam_role_policy_attachment" "cw_logs_full" {
  role       = aws_iam_role.eks_cw_agent_role.name
  policy_arn = "arn:aws:iam::aws:policy/CloudWatchFullAccess"
}

########################################
#     AWS LOAD BALANCER CONTROLLER
########################################
# IAM Policy for ALB Controller
data "aws_iam_policy_document" "alb_controller" {
  statement {
    effect = "Allow"
    actions = [
      "elasticloadbalancing:*",
      "ec2:Describe*",
      "ec2:CreateSecurityGroup",
      "ec2:CreateTags",
      "ec2:AuthorizeSecurityGroupIngress",
      "ec2:RevokeSecurityGroupIngress",
      "iam:CreateServiceLinkedRole",
      "iam:GetServerCertificate",
      "iam:ListServerCertificates"
    ]
    resources = ["*"]
  }
}

resource "aws_iam_policy" "alb_controller" {
  name   = "AWSLoadBalancerControllerIAMPolicy"
  policy = data.aws_iam_policy_document.alb_controller.json
}

resource "aws_iam_role" "alb_controller" {
  name = "aws-load-balancer-controller-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Principal = { Federated = module.eks.oidc_provider_arn }
      Action = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "${module.eks.oidc_provider}:sub" = "system:serviceaccount:kube-system:aws-load-balancer-controller"
        }
      }
    }]
  })
}

resource "aws_iam_role_policy_attachment" "alb_controller_attach" {
  role       = aws_iam_role.alb_controller.name
  policy_arn = aws_iam_policy.alb_controller.arn
}

resource "kubernetes_service_account" "alb_controller" {
  metadata {
    name      = "aws-load-balancer-controller"
    namespace = "kube-system"
    annotations = {
      "eks.amazonaws.com/role-arn" = aws_iam_role.alb_controller.arn
    }
  }
}

resource "helm_release" "alb_controller" {
  name       = "aws-load-balancer-controller"
  repository = "https://aws.github.io/eks-charts"
  chart      = "aws-load-balancer-controller"
  namespace  = "kube-system"

  depends_on = [kubernetes_service_account.alb_controller]

  set { name = "clusterName", value = module.eks.cluster_name }
  set { name = "region",      value = var.region }
  set { name = "vpcId",       value = module.vpc.vpc_id }

  set { name = "serviceAccount.create", value = "false" }
  set { name = "serviceAccount.name",   value = kubernetes_service_account.alb_controller.metadata[0].name }
}

########################################
#          MONITORING NAMESPACE
########################################
resource "kubernetes_namespace" "monitoring" {
  metadata { name = "monitoring" }
}

resource "helm_release" "kube_prometheus_stack" {
  name       = "monitoring"
  repository = "https://prometheus-community.github.io/helm-charts"
  chart      = "kube-prometheus-stack"
  namespace  = kubernetes_namespace.monitoring.metadata[0].name

  values = [
    yamlencode({
      grafana = { adminPassword = "prom-operator" }
    })
  ]

  depends_on = [kubernetes_namespace.monitoring]
}

########################################
#         APPLICATION DEPLOYMENT
########################################
resource "kubernetes_namespace" "session_system" {
  metadata { name = "session-system" }
}

resource "kubernetes_deployment_v1" "api_gateway" {
  metadata {
    name      = "api-gateway"
    namespace = kubernetes_namespace.session_system.metadata[0].name
    labels = { app = "api-gateway" }
  }

  spec {
    replicas = 1
    selector { match_labels = { app = "api-gateway" } }

    template {
      metadata { labels = { app = "api-gateway" } }
      spec {
        container {
          name  = "api-gateway"
          image = "766330623591.dkr.ecr.ap-northeast-1.amazonaws.com/api-gateway:v1"

          port { container_port = 8080 }

          env {
            name  = "ROUTER_URL"
            value = "http://router.session-system.svc.cluster.local"
          }

          readiness_probe {
            http_get { path = "/healthz"; port = 8080 }
            initial_delay_seconds = 3
            period_seconds        = 5
          }

          liveness_probe {
            http_get { path = "/healthz"; port = 8080 }
            initial_delay_seconds = 10
            period_seconds        = 20
          }
        }
      }
    }
  }
}

resource "kubernetes_service_v1" "api_gateway" {
  metadata {
    name      = "api-gateway"
    namespace = kubernetes_namespace.session_system.metadata[0].name
    labels = { app = "api-gateway" }
  }

  spec {
    selector = { app = "api-gateway" }

    port {
      port        = 80
      target_port = 8080
    }

    type = "ClusterIP"
  }
}

resource "kubernetes_ingress_v1" "api_gateway" {
  metadata {
    name      = "api-gateway-ingress"
    namespace = kubernetes_namespace.session_system.metadata[0].name
    annotations = {
      "kubernetes.io/ingress.class"           = "alb"
      "alb.ingress.kubernetes.io/scheme"      = "internet-facing"
      "alb.ingress.kubernetes.io/target-type" = "ip"
    }
  }

  spec {
    rule {
      http {
        path {
          path      = "/"
          path_type = "Prefix"
          backend {
            service {
              name = kubernetes_service_v1.api_gateway.metadata[0].name
              port { number = 80 }
            }
          }
        }
      }
    }
  }

  depends_on = [helm_release.alb_controller]
}

########################################
#           CLOUDWATCH METRICS
########################################
resource "aws_cloudwatch_log_metric_filter" "pod_created" {
  log_group_name = "/aws/containerinsights/${var.cluster_name}/dataplane"
  name           = "PodCreated-session-system"
  pattern        = "\"SyncLoop ADD\" \"session-system\""

  metric_transformation {
    name      = "PodCreatedCount"
    namespace = "Custom/EKS"
    value     = "1"
  }
}

resource "aws_cloudwatch_log_metric_filter" "pod_deleted" {
  log_group_name = "/aws/containerinsights/${var.cluster_name}/dataplane"
  name           = "PodDeleted-session-system"
  pattern        = "\"SyncLoop DELETE\" \"session-system\""

  metric_transformation {
    name      = "PodDeletedCount"
    namespace = "Custom/EKS"
    value     = "1"
  }
}

########################################
#               SNS ALERTING
########################################
resource "aws_sns_topic" "eks_pod_alerts" {
  name = "eks-pod-alerts"
}

resource "aws_sns_topic_subscription" "email_alert" {
  topic_arn = aws_sns_topic.eks_pod_alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

########################################
#              CLOUDWATCH ALARMS
########################################
resource "aws_cloudwatch_metric_alarm" "pod_created_alarm" {
  alarm_name          = "PodCreated-session-system"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  period              = 60
  threshold           = 0
  statistic           = "Sum"
  metric_name         = "PodCreatedCount"
  namespace           = "Custom/EKS"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.eks_pod_alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "pod_deleted_alarm" {
  alarm_name          = "PodDeleted-session-system"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  period              = 60
  threshold           = 0
  statistic           = "Sum"
  metric_name         = "PodDeletedCount"
  namespace           = "Custom/EKS"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.eks_pod_alerts.arn]
}