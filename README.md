# ekstools

A collection of production-grade CLI tools for day-to-day Amazon EKS operations.

---

## Tool: `aws_auth_manager.py`

Manages the `aws-auth` ConfigMap in an EKS cluster — the Kubernetes ConfigMap that
controls which AWS IAM identities can access your cluster.

Supports:
- Adding / removing **IAM users**
- Adding / removing **IAM roles**
- Two access levels: **admin** and **developer**
- `--dry-run` mode to preview changes safely
- No `kubectl` or kubeconfig required — authenticates directly via AWS credentials

---

### How it works

The `aws-auth` ConfigMap lives in the `kube-system` namespace. EKS reads it to
translate incoming AWS IAM identities into Kubernetes users/groups, which RBAC
policies then authorise.

```
IAM User/Role  →  aws-auth ConfigMap  →  Kubernetes group  →  RBAC ClusterRole
```

This tool:
1. Calls the AWS EKS API to get the cluster endpoint and CA certificate.
2. Generates a short-lived bearer token via a pre-signed STS
   `GetCallerIdentity` URL (same as `aws eks get-token`).
3. Uses the Kubernetes Python client to read and update the `aws-auth` ConfigMap.

---

### Access levels

| Flag | Kubernetes group | Effect |
|------|-----------------|--------|
| `--access admin` | `system:masters` | Full cluster-admin. Bound to the built-in `cluster-admin` ClusterRole. |
| `--access developer` | `eks-developers` | Custom group. Requires a ClusterRoleBinding (see below). |

---

### Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.8+ | |
| AWS credentials | Env vars, `~/.aws/credentials`, or instance/pod profile |
| IAM permission: `eks:DescribeCluster` | To fetch endpoint + CA |
| IAM permission: `sts:GetCallerIdentity` | For token generation |
| Kubernetes RBAC: read/write ConfigMaps in `kube-system` | Your IAM identity must already be in the cluster (e.g. cluster creator) |

---

### Installation

```bash
# 1. Clone the repo
git clone <repo-url>
cd ekstools

# 2. Create and activate a virtual environment (recommended)
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt
```

---

### Usage

#### Add an IAM user as admin

```bash
python aws_auth_manager.py add-user \
  --cluster-name my-eks-cluster \
  --user-arn arn:aws:iam::123456789012:user/alice \
  --access admin \
  --region us-east-1
```

#### Add an IAM user as developer

```bash
python aws_auth_manager.py add-user \
  --cluster-name my-eks-cluster \
  --user-arn arn:aws:iam::123456789012:user/bob \
  --access developer \
  --region us-east-1
```

Optionally override the Kubernetes username (defaults to the IAM user name):

```bash
python aws_auth_manager.py add-user \
  --cluster-name my-eks-cluster \
  --user-arn arn:aws:iam::123456789012:user/bob \
  --username bob-dev \
  --access developer
```

#### Add an IAM role as admin

```bash
python aws_auth_manager.py add-role \
  --cluster-name my-eks-cluster \
  --role-arn arn:aws:iam::123456789012:role/MyAdminRole \
  --access admin
```

#### Add an IAM role as developer

```bash
python aws_auth_manager.py add-role \
  --cluster-name my-eks-cluster \
  --role-arn arn:aws:iam::123456789012:role/MyDevRole \
  --access developer
```

#### List current aws-auth entries

```bash
python aws_auth_manager.py list \
  --cluster-name my-eks-cluster \
  --region us-east-1
```

Sample output:

```
────────────────────────────────────────────────────────────────
  aws-auth  |  cluster: my-eks-cluster  |  region: us-east-1
────────────────────────────────────────────────────────────────

  IAM Users (mapUsers)
  ··················
  userarn  : arn:aws:iam::123456789012:user/alice
  username : alice
  groups   : system:masters

  userarn  : arn:aws:iam::123456789012:user/bob
  username : bob
  groups   : eks-developers

  IAM Roles (mapRoles)
  ··················
  rolearn  : arn:aws:iam::123456789012:role/MyNodeRole
  username : system:node:{{EC2PrivateDNSName}}
  groups   : system:bootstrappers, system:nodes
```

#### Remove an IAM user

```bash
python aws_auth_manager.py remove-user \
  --cluster-name my-eks-cluster \
  --user-arn arn:aws:iam::123456789012:user/bob
```

#### Remove an IAM role

```bash
python aws_auth_manager.py remove-role \
  --cluster-name my-eks-cluster \
  --role-arn arn:aws:iam::123456789012:role/MyDevRole
```

#### Dry-run (preview without applying)

Any `add-*` or `remove-*` command accepts `--dry-run`:

```bash
python aws_auth_manager.py add-user \
  --cluster-name my-eks-cluster \
  --user-arn arn:aws:iam::123456789012:user/charlie \
  --access admin \
  --dry-run
```

#### Debug / verbose logging

```bash
python aws_auth_manager.py add-user ... --verbose
```

---

### Setting up developer RBAC

The `developer` access level places the IAM identity into the `eks-developers`
Kubernetes group. You must create a ClusterRoleBinding (or RoleBinding for
namespace-scoped access) to grant actual permissions.

**Cluster-wide read-only access (view):**

```bash
kubectl create clusterrolebinding eks-developers \
  --clusterrole=view \
  --group=eks-developers
```

**Namespace-scoped edit access:**

```bash
kubectl create rolebinding eks-developers-edit \
  --namespace=my-app \
  --clusterrole=edit \
  --group=eks-developers
```

**Custom ClusterRole example** (`developer-clusterrole.yaml`):

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: eks-developer
rules:
  - apiGroups: ["", "apps", "batch", "extensions"]
    resources:
      - pods
      - pods/log
      - deployments
      - replicasets
      - services
      - configmaps
      - jobs
      - cronjobs
    verbs: ["get", "list", "watch"]
  - apiGroups: [""]
    resources: ["pods/exec"]
    verbs: ["create"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: eks-developers
subjects:
  - kind: Group
    name: eks-developers
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: ClusterRole
  name: eks-developer
  apiGroup: rbac.authorization.k8s.io
```

Apply it:

```bash
kubectl apply -f developer-clusterrole.yaml
```

---

### AWS credentials setup

The tool uses standard AWS credential resolution in this order:

1. Environment variables: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`
2. AWS profile: `~/.aws/credentials` (use `AWS_PROFILE` to select a profile)
3. IAM instance/container role (EC2, ECS, Lambda, EKS Pod Identity)

Set the region via `--region` flag or the `AWS_DEFAULT_REGION` environment variable:

```bash
export AWS_DEFAULT_REGION=us-west-2
export AWS_PROFILE=my-prod-profile
python aws_auth_manager.py list --cluster-name my-cluster
```

---

### Required IAM permissions

Attach the following IAM policy to the identity running this tool:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "EKSDescribe",
      "Effect": "Allow",
      "Action": "eks:DescribeCluster",
      "Resource": "arn:aws:eks:*:*:cluster/*"
    },
    {
      "Sid": "STSGetCallerIdentity",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    }
  ]
}
```

The identity must also have Kubernetes RBAC rights to read and write ConfigMaps in
`kube-system`. The cluster creator automatically has these rights. For other
identities, apply:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: aws-auth-manager
rules:
  - apiGroups: [""]
    resources: ["configmaps"]
    resourceNames: ["aws-auth"]
    namespaces: ["kube-system"]
    verbs: ["get", "update"]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata:
  name: aws-auth-manager
subjects:
  - kind: User
    name: <kubernetes-username-of-the-operator>
    apiGroup: rbac.authorization.k8s.io
roleRef:
  kind: ClusterRole
  name: aws-auth-manager
  apiGroup: rbac.authorization.k8s.io
```

---

### Help

```bash
python aws_auth_manager.py --help
python aws_auth_manager.py add-user --help
python aws_auth_manager.py add-role --help
python aws_auth_manager.py remove-user --help
python aws_auth_manager.py remove-role --help
python aws_auth_manager.py list --help
```

---

### Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `EKS cluster 'X' not found` | Wrong name or region | Check `--cluster-name` and `--region` |
| `AWS credentials/config error` | No credentials found | Set env vars or configure `~/.aws/credentials` |
| `aws-auth ConfigMap not found` | No worker nodes ever registered | Register a node group first |
| `Permission denied reading aws-auth` | IAM identity not yet in the cluster | Use the cluster-creator credentials first |
| `already present in aws-auth` | Duplicate entry | Run `remove-user` / `remove-role` first, then re-add |
| `is not ACTIVE` | Cluster is creating/deleting | Wait for cluster to reach ACTIVE state |

