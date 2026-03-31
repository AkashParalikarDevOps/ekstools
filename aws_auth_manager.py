#!/usr/bin/env python3
"""
aws_auth_manager.py
-------------------
Production-grade CLI to manage the EKS aws-auth ConfigMap.

Supports adding/removing IAM users and IAM roles with two access levels:
  - admin     → Kubernetes group: system:masters  (full cluster-admin)
  - developer → Kubernetes group: eks-developers  (requires ClusterRoleBinding)

Authentication uses a presigned STS URL (same mechanism as `aws eks get-token`),
so no kubeconfig or kubectl installation is required.
"""

import base64
import logging
import os
import re
import sys
import tempfile
from typing import Dict, List, Optional, Tuple

import boto3
import click
import yaml
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError
from botocore.signers import RequestSigner
from kubernetes import client as k8s_client
from kubernetes.client.rest import ApiException

# ─── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)-8s] %(message)s", "%Y-%m-%dT%H:%M:%S")
    )
    root = logging.getLogger("aws_auth_manager")
    root.setLevel(level)
    if not root.handlers:
        root.addHandler(handler)


logger = logging.getLogger("aws_auth_manager")

# ─── Constants ────────────────────────────────────────────────────────────────

CONFIGMAP_NAME      = "aws-auth"
CONFIGMAP_NAMESPACE = "kube-system"
STS_TOKEN_EXPIRES_IN = 60  # seconds

ACCESS_LEVELS: Dict[str, Dict] = {
    "admin": {
        "groups":      ["system:masters"],
        "description": "Full cluster-admin access (system:masters group).",
    },
    "developer": {
        "groups":      ["eks-developers"],
        "description": (
            "Developer access (eks-developers group). "
            "Requires a ClusterRoleBinding — see README."
        ),
    },
}

_IAM_USER_ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:user/[\w+=,.@/-]+$")
_IAM_ROLE_ARN_RE = re.compile(r"^arn:aws[a-z-]*:iam::\d{12}:role/[\w+=,.@/-]+$")

# ─── AWS / EKS helpers ────────────────────────────────────────────────────────

def _get_cluster_info(cluster_name: str, region: str) -> Dict:
    """
    Describe the EKS cluster and return its API endpoint and CA certificate.
    Raises ClickException on any error so Click prints a clean message.
    """
    eks = boto3.client("eks", region_name=region)
    try:
        resp = eks.describe_cluster(name=cluster_name)
    except ClientError as exc:
        code = exc.response["Error"]["Code"]
        if code == "ResourceNotFoundException":
            raise click.ClickException(
                f"EKS cluster '{cluster_name}' not found in region '{region}'."
            )
        raise click.ClickException(f"AWS error while describing cluster: {exc}")
    except (NoCredentialsError, BotoCoreError) as exc:
        raise click.ClickException(f"AWS credentials/config error: {exc}")

    cluster = resp["cluster"]
    if cluster["status"] != "ACTIVE":
        raise click.ClickException(
            f"Cluster '{cluster_name}' is not ACTIVE (current status: {cluster['status']})."
        )

    logger.debug("Cluster endpoint: %s", cluster["endpoint"])
    return {
        "endpoint": cluster["endpoint"],
        "ca_data":  cluster["certificateAuthority"]["data"],
    }


def _generate_eks_token(cluster_name: str, region: str) -> str:
    """
    Generate a pre-signed STS GetCallerIdentity bearer token accepted by the
    EKS API server.  This is identical to what `aws eks get-token` produces.
    """
    session = boto3.session.Session()
    sts = session.client("sts", region_name=region)
    service_id = sts.meta.service_model.service_id

    signer = RequestSigner(
        service_id, region, "sts", "v4",
        session.get_credentials(), session.events,
    )
    params = {
        "method":  "GET",
        "url":     (
            f"https://sts.{region}.amazonaws.com/"
            "?Action=GetCallerIdentity&Version=2011-06-15"
        ),
        "body":    {},
        "headers": {"x-k8s-aws-id": cluster_name},
        "context": {},
    }
    url = signer.generate_presigned_url(
        params, region_name=region,
        expires_in=STS_TOKEN_EXPIRES_IN, operation_name="",
    )
    token = "k8s-aws-v1." + base64.urlsafe_b64encode(
        url.encode("utf-8")
    ).decode("utf-8").rstrip("=")
    logger.debug("EKS bearer token generated (expires in %ds)", STS_TOKEN_EXPIRES_IN)
    return token


# ─── Kubernetes helpers ───────────────────────────────────────────────────────

def _build_k8s_client(cluster_name: str, region: str) -> Tuple[k8s_client.CoreV1Api, str]:
    """
    Build an authenticated Kubernetes CoreV1Api for the given EKS cluster.

    Returns (api_client, tmp_ca_cert_path).
    The caller MUST delete tmp_ca_cert_path when done (use try/finally).
    """
    info  = _get_cluster_info(cluster_name, region)
    token = _generate_eks_token(cluster_name, region)

    # kubernetes-client requires the CA cert as a file path, not bytes.
    ca_bytes = base64.b64decode(info["ca_data"])
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
    tmp.write(ca_bytes)
    tmp.flush()
    tmp.close()

    cfg = k8s_client.Configuration()
    cfg.host                            = info["endpoint"]
    cfg.verify_ssl                      = True
    cfg.ssl_ca_cert                     = tmp.name
    cfg.api_key["authorization"]        = token
    cfg.api_key_prefix["authorization"] = "Bearer"

    return k8s_client.CoreV1Api(k8s_client.ApiClient(cfg)), tmp.name


def _read_configmap(v1: k8s_client.CoreV1Api) -> k8s_client.V1ConfigMap:
    try:
        return v1.read_namespaced_config_map(
            name=CONFIGMAP_NAME, namespace=CONFIGMAP_NAMESPACE
        )
    except ApiException as exc:
        if exc.status == 404:
            raise click.ClickException(
                "aws-auth ConfigMap not found in kube-system. "
                "Has the cluster had any worker nodes registered?"
            )
        if exc.status == 403:
            raise click.ClickException(
                "Permission denied reading aws-auth ConfigMap. "
                "Ensure your IAM identity has eks:DescribeCluster and "
                "kubernetes RBAC access to ConfigMaps in kube-system."
            )
        raise click.ClickException(f"Kubernetes API error: {exc.reason} (status {exc.status})")


def _parse_configmap(
    cm: k8s_client.V1ConfigMap,
) -> Tuple[List[Dict], List[Dict]]:
    """Return (map_users, map_roles) safely parsed from ConfigMap data."""
    data = cm.data or {}
    map_users = yaml.safe_load(data.get("mapUsers") or "") or []
    map_roles = yaml.safe_load(data.get("mapRoles") or "") or []

    if not isinstance(map_users, list):
        raise click.ClickException("aws-auth mapUsers is not a YAML list — manual inspection required.")
    if not isinstance(map_roles, list):
        raise click.ClickException("aws-auth mapRoles is not a YAML list — manual inspection required.")

    return map_users, map_roles


def _write_configmap(
    v1: k8s_client.CoreV1Api,
    cm: k8s_client.V1ConfigMap,
    map_users: List[Dict],
    map_roles: List[Dict],
) -> None:
    if cm.data is None:
        cm.data = {}

    # Serialise; omit key entirely if list is empty to keep ConfigMap clean.
    cm.data["mapUsers"] = (
        yaml.dump(map_users, default_flow_style=False) if map_users else ""
    )
    cm.data["mapRoles"] = (
        yaml.dump(map_roles, default_flow_style=False) if map_roles else ""
    )

    try:
        v1.replace_namespaced_config_map(
            name=CONFIGMAP_NAME, namespace=CONFIGMAP_NAMESPACE, body=cm
        )
        logger.debug("aws-auth ConfigMap replaced successfully.")
    except ApiException as exc:
        raise click.ClickException(
            f"Failed to update aws-auth ConfigMap: {exc.reason} (status {exc.status})"
        )


# ─── Core operations ─────────────────────────────────────────────────────────

def _op_add_user(
    cluster_name: str,
    user_arn: str,
    username: Optional[str],
    access: str,
    region: str,
    dry_run: bool,
) -> None:
    if not _IAM_USER_ARN_RE.match(user_arn):
        raise click.ClickException(
            f"Invalid IAM user ARN: '{user_arn}'. "
            "Expected format: arn:aws:iam::ACCOUNT_ID:user/USERNAME"
        )

    resolved_username = username or user_arn.split("/")[-1]
    groups = ACCESS_LEVELS[access]["groups"]

    new_entry: Dict = {
        "userarn":  user_arn,
        "username": resolved_username,
        "groups":   groups,
    }

    logger.info(
        "Adding IAM user  arn=%s  username=%s  access=%s  groups=%s",
        user_arn, resolved_username, access, groups,
    )

    v1, ca_path = _build_k8s_client(cluster_name, region)
    try:
        cm = _read_configmap(v1)
        map_users, map_roles = _parse_configmap(cm)

        # Idempotency check
        for entry in map_users:
            if entry.get("userarn") == user_arn:
                raise click.ClickException(
                    f"IAM user '{user_arn}' is already present in aws-auth. "
                    "Use remove-user first if you want to change access."
                )

        map_users.append(new_entry)

        if dry_run:
            click.echo("[DRY-RUN] Would append to mapUsers:")
            click.echo(yaml.dump([new_entry], default_flow_style=False))
            return

        _write_configmap(v1, cm, map_users, map_roles)
    finally:
        os.unlink(ca_path)

    click.echo(
        f"[OK] IAM user '{user_arn}' added as '{access}' "
        f"(Kubernetes groups: {', '.join(groups)})."
    )
    if access == "developer":
        _print_rbac_hint("user", resolved_username)


def _op_add_role(
    cluster_name: str,
    role_arn: str,
    username: Optional[str],
    access: str,
    region: str,
    dry_run: bool,
) -> None:
    if not _IAM_ROLE_ARN_RE.match(role_arn):
        raise click.ClickException(
            f"Invalid IAM role ARN: '{role_arn}'. "
            "Expected format: arn:aws:iam::ACCOUNT_ID:role/ROLENAME"
        )

    resolved_username = username or role_arn.split("/")[-1]
    groups = ACCESS_LEVELS[access]["groups"]

    new_entry: Dict = {
        "rolearn":  role_arn,
        "username": resolved_username,
        "groups":   groups,
    }

    logger.info(
        "Adding IAM role  arn=%s  username=%s  access=%s  groups=%s",
        role_arn, resolved_username, access, groups,
    )

    v1, ca_path = _build_k8s_client(cluster_name, region)
    try:
        cm = _read_configmap(v1)
        map_users, map_roles = _parse_configmap(cm)

        for entry in map_roles:
            if entry.get("rolearn") == role_arn:
                raise click.ClickException(
                    f"IAM role '{role_arn}' is already present in aws-auth. "
                    "Use remove-role first if you want to change access."
                )

        map_roles.append(new_entry)

        if dry_run:
            click.echo("[DRY-RUN] Would append to mapRoles:")
            click.echo(yaml.dump([new_entry], default_flow_style=False))
            return

        _write_configmap(v1, cm, map_users, map_roles)
    finally:
        os.unlink(ca_path)

    click.echo(
        f"[OK] IAM role '{role_arn}' added as '{access}' "
        f"(Kubernetes groups: {', '.join(groups)})."
    )
    if access == "developer":
        _print_rbac_hint("role", resolved_username)


def _op_remove_user(
    cluster_name: str, user_arn: str, region: str, dry_run: bool
) -> None:
    v1, ca_path = _build_k8s_client(cluster_name, region)
    try:
        cm = _read_configmap(v1)
        map_users, map_roles = _parse_configmap(cm)

        filtered = [e for e in map_users if e.get("userarn") != user_arn]
        if len(filtered) == len(map_users):
            raise click.ClickException(
                f"IAM user '{user_arn}' not found in aws-auth ConfigMap."
            )

        if dry_run:
            click.echo(f"[DRY-RUN] Would remove user '{user_arn}' from mapUsers.")
            return

        _write_configmap(v1, cm, filtered, map_roles)
    finally:
        os.unlink(ca_path)

    click.echo(f"[OK] IAM user '{user_arn}' removed from aws-auth ConfigMap.")


def _op_remove_role(
    cluster_name: str, role_arn: str, region: str, dry_run: bool
) -> None:
    v1, ca_path = _build_k8s_client(cluster_name, region)
    try:
        cm = _read_configmap(v1)
        map_users, map_roles = _parse_configmap(cm)

        filtered = [e for e in map_roles if e.get("rolearn") != role_arn]
        if len(filtered) == len(map_roles):
            raise click.ClickException(
                f"IAM role '{role_arn}' not found in aws-auth ConfigMap."
            )

        if dry_run:
            click.echo(f"[DRY-RUN] Would remove role '{role_arn}' from mapRoles.")
            return

        _write_configmap(v1, cm, map_users, filtered)
    finally:
        os.unlink(ca_path)

    click.echo(f"[OK] IAM role '{role_arn}' removed from aws-auth ConfigMap.")


def _op_list(cluster_name: str, region: str) -> None:
    v1, ca_path = _build_k8s_client(cluster_name, region)
    try:
        cm = _read_configmap(v1)
        map_users, map_roles = _parse_configmap(cm)
    finally:
        os.unlink(ca_path)

    sep = "─" * 64
    click.echo(f"\n{sep}")
    click.echo(f"  aws-auth  |  cluster: {cluster_name}  |  region: {region}")
    click.echo(sep)

    click.echo("\n  IAM Users (mapUsers)\n  " + "·" * 30)
    if map_users:
        for u in map_users:
            click.echo(f"  userarn  : {u.get('userarn', '(missing)')}")
            click.echo(f"  username : {u.get('username', '(missing)')}")
            click.echo(f"  groups   : {', '.join(u.get('groups', []))}")
            click.echo()
    else:
        click.echo("  (none)\n")

    click.echo("  IAM Roles (mapRoles)\n  " + "·" * 30)
    if map_roles:
        for r in map_roles:
            click.echo(f"  rolearn  : {r.get('rolearn', '(missing)')}")
            click.echo(f"  username : {r.get('username', '(missing)')}")
            click.echo(f"  groups   : {', '.join(r.get('groups', []))}")
            click.echo()
    else:
        click.echo("  (none)\n")

    click.echo(sep + "\n")


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _print_rbac_hint(principal_type: str, username: str) -> None:
    click.echo(
        "\nNOTE: Developer access uses the 'eks-developers' Kubernetes group.\n"
        "You must create a ClusterRoleBinding to grant actual permissions.\n"
        "Example (view-only access across all namespaces):\n\n"
        "  kubectl create clusterrolebinding eks-developers \\\n"
        "    --clusterrole=view \\\n"
        "    --group=eks-developers\n\n"
        "Replace --clusterrole=view with your desired ClusterRole.\n"
        "See the README for a namespace-scoped developer role example.\n"
    )


# ─── CLI definition ───────────────────────────────────────────────────────────

def _common_options(func):
    """Decorator: attach --cluster-name, --region, --dry-run, --verbose to a command."""
    decorators = [
        click.option(
            "--cluster-name", required=True,
            help="Name of the EKS cluster.",
        ),
        click.option(
            "--region",
            default=lambda: os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
            show_default="AWS_DEFAULT_REGION or us-east-1",
            help="AWS region where the cluster lives.",
        ),
        click.option(
            "--dry-run", is_flag=True, default=False,
            help="Print what would change without applying it.",
        ),
        click.option(
            "--verbose", is_flag=True, default=False,
            help="Enable DEBUG-level logging.",
        ),
    ]
    for dec in reversed(decorators):
        func = dec(func)
    return func


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
def cli():
    """
    Manage the aws-auth ConfigMap for an Amazon EKS cluster.

    \b
    Authentication is handled automatically via your AWS credentials
    (env vars, ~/.aws/credentials, or EC2/ECS instance profile).
    No kubeconfig or kubectl installation is required.
    """


@cli.command("add-user")
@_common_options
@click.option(
    "--user-arn", required=True,
    help="Full ARN of the IAM user.  e.g. arn:aws:iam::123456789012:user/alice",
)
@click.option(
    "--username", default=None,
    help="Kubernetes username to map to (defaults to the IAM user name).",
)
@click.option(
    "--access",
    type=click.Choice(["admin", "developer"], case_sensitive=False),
    required=True,
    help=(
        "admin     → system:masters group (full cluster-admin).\n"
        "developer → eks-developers group (needs ClusterRoleBinding)."
    ),
)
def cmd_add_user(cluster_name, user_arn, username, access, region, dry_run, verbose):
    """Add an IAM user to the aws-auth ConfigMap."""
    _setup_logging(verbose)
    _op_add_user(cluster_name, user_arn.strip(), username, access.lower(), region, dry_run)


@cli.command("add-role")
@_common_options
@click.option(
    "--role-arn", required=True,
    help="Full ARN of the IAM role.  e.g. arn:aws:iam::123456789012:role/my-role",
)
@click.option(
    "--username", default=None,
    help="Kubernetes username to map to (defaults to the IAM role name).",
)
@click.option(
    "--access",
    type=click.Choice(["admin", "developer"], case_sensitive=False),
    required=True,
    help=(
        "admin     → system:masters group (full cluster-admin).\n"
        "developer → eks-developers group (needs ClusterRoleBinding)."
    ),
)
def cmd_add_role(cluster_name, role_arn, username, access, region, dry_run, verbose):
    """Add an IAM role to the aws-auth ConfigMap."""
    _setup_logging(verbose)
    _op_add_role(cluster_name, role_arn.strip(), username, access.lower(), region, dry_run)


@cli.command("remove-user")
@_common_options
@click.option(
    "--user-arn", required=True,
    help="Full ARN of the IAM user to remove.",
)
def cmd_remove_user(cluster_name, user_arn, region, dry_run, verbose):
    """Remove an IAM user from the aws-auth ConfigMap."""
    _setup_logging(verbose)
    _op_remove_user(cluster_name, user_arn.strip(), region, dry_run)


@cli.command("remove-role")
@_common_options
@click.option(
    "--role-arn", required=True,
    help="Full ARN of the IAM role to remove.",
)
def cmd_remove_role(cluster_name, role_arn, region, dry_run, verbose):
    """Remove an IAM role from the aws-auth ConfigMap."""
    _setup_logging(verbose)
    _op_remove_role(cluster_name, role_arn.strip(), region, dry_run)


@cli.command("list")
@click.option(
    "--cluster-name", required=True,
    help="Name of the EKS cluster.",
)
@click.option(
    "--region",
    default=lambda: os.environ.get("AWS_DEFAULT_REGION", "us-east-1"),
    show_default="AWS_DEFAULT_REGION or us-east-1",
    help="AWS region where the cluster lives.",
)
@click.option("--verbose", is_flag=True, default=False, help="Enable DEBUG-level logging.")
def cmd_list(cluster_name, region, verbose):
    """List all IAM users and roles currently in the aws-auth ConfigMap."""
    _setup_logging(verbose)
    _op_list(cluster_name, region)


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli()
