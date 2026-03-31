# ekstools

A collection of production-grade CLI tools for day-to-day Amazon EKS operations.

---

## Tool: `aws_auth_manager.py`

Manages the `aws-auth` ConfigMap in an EKS cluster — the Kubernetes ConfigMap that
controls which AWS IAM identities can access your cluster.

Supports:
- Adding / removing **IAM users** and **IAM roles** individually
- **GitOps-style sync** from a desired-state YAML file (`iam-auth.yaml`)
- Two access levels: **admin** and **developer**
- `--dry-run` mode to preview changes safely
- No `kubectl` or kubeconfig required — authenticates directly via AWS credentials
- Production-grade **GitHub Actions pipeline** for automated reconciliation

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

You may also supply a `groups:` list directly in `iam-auth.yaml` to use custom
groups (e.g. for node group roles that need `system:bootstrappers`).

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

### Commands

| Command | Description |
|---------|-------------|
| `sync` | Reconcile aws-auth to match a desired-state YAML file (GitOps) |
| `add-user` | Add a single IAM user |
| `add-role` | Add a single IAM role |
| `remove-user` | Remove a single IAM user |
| `remove-role` | Remove a single IAM role |
| `list` | List all current aws-auth entries |

---

### `sync` — GitOps reconciliation (recommended)

The `sync` command treats `iam-auth.yaml` as the **single source of truth**:

- Entries in the file but not in the cluster → **added**
- Entries in the cluster but not in the file → **removed**
- Entries present in both but with different config → **updated**

A Terraform-style plan is always printed before any write.

#### 1. Define desired state in `iam-auth.yaml`

```yaml
users:
  # Cluster administrator — full system:masters access
  - arn: arn:aws:iam::123456789012:user/alice
    access: admin
    username: alice          # optional; defaults to last ARN segment

  # Developer — eks-developers group (ClusterRoleBinding required)
  - arn: arn:aws:iam::123456789012:user/bob
    access: developer

roles:
  # CI/CD pipeline role
  - arn: arn:aws:iam::123456789012:role/github-actions-role
    access: developer
    username: github-ci

  # Node group role — use explicit groups to override the 'access' shorthand
  - arn: arn:aws:iam::123456789012:role/eks-node-group-role
    username: "system:node:{{EC2PrivateDNSName}}"
    groups:
      - system:bootstrappers
      - system:nodes
```

**`iam-auth.yaml` field reference:**

| Field | Required | Description |
|-------|----------|-------------|
| `arn` | Yes | Full IAM user or role ARN |
| `access` | Yes* | `admin` or `developer` (*not required if `groups` is set) |
| `groups` | Yes* | Explicit Kubernetes groups list (*overrides `access` if both are set) |
| `username` | No | Kubernetes username; defaults to the last path segment of the ARN |

#### 2. Preview the changes (dry-run)

```bash
python aws_auth_manager.py sync \
  --cluster-name my-eks-cluster \
  --region us-east-1 \
  --file iam-auth.yaml \
  --dry-run
```

Sample output:

```
────────────────────────────────────────────────────────────────
  aws-auth  |  SYNC PLAN
────────────────────────────────────────────────────────────────

  IAM Users (mapUsers)
  ······························
  + ADD
    arn      : arn:aws:iam::123456789012:user/alice
    username : alice
    groups   : system:masters

  - REMOVE
    arn      : arn:aws:iam::123456789012:user/old-user
    username : old-user
    groups   : eks-developers

  IAM Roles (mapRoles)
  ······························
  ~ UPDATE
    arn      : arn:aws:iam::123456789012:role/github-actions-role
    groups   : ['system:masters'] → ['eks-developers']

────────────────────────────────────────────────────────────────
  Plan: 1 to add, 1 to remove, 1 to update.
────────────────────────────────────────────────────────────────

[DRY-RUN] No changes applied.
```

#### 3. Apply

```bash
python aws_auth_manager.py sync \
  --cluster-name my-eks-cluster \
  --region us-east-1 \
  --file iam-auth.yaml
```

---

### Individual commands

#### Add an IAM user

```bash
python aws_auth_manager.py add-user \
  --cluster-name my-eks-cluster \
  --user-arn arn:aws:iam::123456789012:user/alice \
  --access admin \
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

#### Add an IAM role

```bash
python aws_auth_manager.py add-role \
  --cluster-name my-eks-cluster \
  --role-arn arn:aws:iam::123456789012:role/MyAdminRole \
  --access admin
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

#### List current aws-auth entries

```bash
python aws_auth_manager.py list \
  --cluster-name my-eks-cluster \
  --region us-east-1
```

#### Dry-run any command

All commands accept `--dry-run` to preview changes without applying:

```bash
python aws_auth_manager.py add-user \
  --cluster-name my-eks-cluster \
  --user-arn arn:aws:iam::123456789012:user/charlie \
  --access admin \
  --dry-run
```

#### Verbose / debug logging

```bash
python aws_auth_manager.py sync ... --verbose
```

---

### GitHub Actions pipeline

The pipeline in `.github/workflows/sync-eks-auth.yml` automates sync via GitOps:
edit `iam-auth.yaml`, open a PR, and the plan is posted as a comment. Merge to
`main` and the cluster is reconciled automatically.

```
PR opened/updated
  └─▶ validate  (schema check — no AWS)
  └─▶ plan      (dry-run against live cluster)
  └─▶ comment   (posts plan to PR)

Merge to main
  └─▶ validate
  └─▶ plan
  └─▶ apply     (reconciles cluster — requires "production" env approval)
```

#### Setup

**1. Configure OIDC trust between GitHub Actions and AWS**

Create an IAM role with the following trust policy (replace `ACCOUNT_ID`,
`GITHUB_ORG`, and `REPO_NAME`):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:GITHUB_ORG/REPO_NAME:*"
        }
      }
    }
  ]
}
```

Attach the IAM policy from the [Required IAM permissions](#required-iam-permissions)
section to this role.

**2. Add GitHub secrets and variables**

| Name | Type | Value |
|------|------|-------|
| `AWS_ROLE_ARN` | Secret | ARN of the OIDC role created above |
| `EKS_CLUSTER_NAME` | Variable | Name of your EKS cluster |
| `EKS_REGION` | Variable | AWS region (e.g. `us-east-1`) |

Set at: **Settings → Secrets and variables → Actions**

**3. Create the `production` GitHub environment**

Go to **Settings → Environments → New environment**, name it `production`, and
add at least one required reviewer. The `apply` job will pause for approval
before writing the ConfigMap.

**4. Workflow triggers**

| Event | Jobs run |
|-------|----------|
| PR to `main` (changes to `iam-auth.yaml` or `aws_auth_manager.py`) | validate → plan → comment |
| Push to `main` (same paths) | validate → plan → apply |
| Manual dispatch (`dry_run=true`) | validate → plan |
| Manual dispatch (`dry_run=false`) | validate → plan → apply |

#### Multi-cluster support

To manage multiple clusters, create per-cluster YAML files and extend the
workflow matrix. See the `MULTI-CLUSTER SUPPORT` section at the bottom of
`.github/workflows/sync-eks-auth.yml` for a complete example.

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
python aws_auth_manager.py sync --cluster-name my-cluster --file iam-auth.yaml
```

---

### Required IAM permissions

Attach the following policy to the identity (or OIDC role) running this tool:

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
python aws_auth_manager.py sync --help
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
| `Invalid IAM user/role ARN` | Malformed ARN in `iam-auth.yaml` | Verify ARN format: `arn:aws:iam::ACCOUNT:user/NAME` |
| `must specify 'access' or 'groups'` | Entry missing required field | Add `access: admin` or `access: developer` to the entry |
| `Duplicate ARN in desired-state file` | Same ARN listed twice | Remove the duplicate from `iam-auth.yaml` |
