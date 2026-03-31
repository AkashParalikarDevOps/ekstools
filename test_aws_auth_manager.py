"""
test_aws_auth_manager.py
------------------------
Unit tests for aws_auth_manager.py.

All AWS and Kubernetes calls are mocked — no real credentials or cluster needed.

Run with:
    pip install pytest pytest-mock
    pytest test_aws_auth_manager.py -v
"""

import base64
import os
import tempfile
from unittest.mock import MagicMock, patch, call

import pytest
import yaml
from click.testing import CliRunner
from botocore.exceptions import ClientError, NoCredentialsError

import aws_auth_manager as am
from aws_auth_manager import (
    _IAM_USER_ARN_RE,
    _IAM_ROLE_ARN_RE,
    _parse_configmap,
    _write_configmap,
    _op_add_user,
    _op_add_role,
    _op_remove_user,
    _op_remove_role,
    _op_list,
    cli,
    CONFIGMAP_NAME,
    CONFIGMAP_NAMESPACE,
)
from kubernetes.client.rest import ApiException


# ─── Fixtures ─────────────────────────────────────────────────────────────────

VALID_USER_ARN  = "arn:aws:iam::123456789012:user/alice"
VALID_ROLE_ARN  = "arn:aws:iam::123456789012:role/my-role"
CLUSTER_NAME    = "test-cluster"
REGION          = "us-east-1"

FAKE_CA_B64 = base64.b64encode(b"fake-ca-cert-data").decode()


def _make_cm(map_users=None, map_roles=None):
    """Return a mock V1ConfigMap with the given mapUsers/mapRoles."""
    from kubernetes import client as k8s_client
    cm = MagicMock(spec=k8s_client.V1ConfigMap)
    data = {}
    if map_users is not None:
        data["mapUsers"] = yaml.dump(map_users, default_flow_style=False)
    if map_roles is not None:
        data["mapRoles"] = yaml.dump(map_roles, default_flow_style=False)
    cm.data = data
    return cm


def _mock_k8s_client(cm):
    """Return a (v1_mock, ca_path) pair; v1_mock.read_namespaced_config_map returns cm."""
    v1 = MagicMock()
    v1.read_namespaced_config_map.return_value = cm

    # Create a real temp file so os.unlink in the code under test succeeds
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".crt")
    tmp.close()

    return v1, tmp.name


# ─── ARN regex tests ──────────────────────────────────────────────────────────

class TestArnRegex:
    VALID_USER_ARNS = [
        "arn:aws:iam::123456789012:user/alice",
        "arn:aws:iam::000000000001:user/org/team/bob",
        "arn:aws-cn:iam::123456789012:user/charlie",
        "arn:aws-us-gov:iam::123456789012:user/dave",
        "arn:aws:iam::123456789012:user/user.name+tag@example.com",
    ]
    INVALID_USER_ARNS = [
        "",
        "arn:aws:iam::123456789012:role/my-role",   # role, not user
        "arn:aws:iam::12345:user/alice",             # short account id
        "arn:aws:s3:::my-bucket",                   # wrong service
        "not-an-arn",
        "arn:aws:iam::123456789012:user/",           # empty name
    ]

    VALID_ROLE_ARNS = [
        "arn:aws:iam::123456789012:role/my-role",
        "arn:aws-cn:iam::123456789012:role/service/worker",
        "arn:aws:iam::123456789012:role/role.name+extra",
    ]
    INVALID_ROLE_ARNS = [
        "",
        "arn:aws:iam::123456789012:user/alice",
        "arn:aws:iam::12345:role/r",
        "not-an-arn",
        "arn:aws:iam::123456789012:role/",
    ]

    @pytest.mark.parametrize("arn", VALID_USER_ARNS)
    def test_valid_user_arn(self, arn):
        assert _IAM_USER_ARN_RE.match(arn), f"Expected match for: {arn}"

    @pytest.mark.parametrize("arn", INVALID_USER_ARNS)
    def test_invalid_user_arn(self, arn):
        assert not _IAM_USER_ARN_RE.match(arn), f"Expected no match for: {arn}"

    @pytest.mark.parametrize("arn", VALID_ROLE_ARNS)
    def test_valid_role_arn(self, arn):
        assert _IAM_ROLE_ARN_RE.match(arn), f"Expected match for: {arn}"

    @pytest.mark.parametrize("arn", INVALID_ROLE_ARNS)
    def test_invalid_role_arn(self, arn):
        assert not _IAM_ROLE_ARN_RE.match(arn), f"Expected no match for: {arn}"


# ─── _parse_configmap ─────────────────────────────────────────────────────────

class TestParseConfigmap:
    def test_parse_empty_data(self):
        cm = _make_cm(map_users=[], map_roles=[])
        users, roles = _parse_configmap(cm)
        assert users == []
        assert roles == []

    def test_parse_none_data(self):
        from kubernetes import client as k8s_client
        cm = MagicMock(spec=k8s_client.V1ConfigMap)
        cm.data = None
        users, roles = _parse_configmap(cm)
        assert users == []
        assert roles == []

    def test_parse_users_and_roles(self):
        users_in = [{"userarn": VALID_USER_ARN, "username": "alice", "groups": ["system:masters"]}]
        roles_in = [{"rolearn": VALID_ROLE_ARN, "username": "my-role", "groups": ["eks-developers"]}]
        cm = _make_cm(map_users=users_in, map_roles=roles_in)
        users, roles = _parse_configmap(cm)
        assert users == users_in
        assert roles == roles_in

    def test_parse_missing_keys_returns_empty_lists(self):
        from kubernetes import client as k8s_client
        cm = MagicMock(spec=k8s_client.V1ConfigMap)
        cm.data = {}
        users, roles = _parse_configmap(cm)
        assert users == []
        assert roles == []

    def test_parse_invalid_map_users_raises(self):
        from kubernetes import client as k8s_client
        import click
        cm = MagicMock(spec=k8s_client.V1ConfigMap)
        # A valid YAML mapping (dict) — not a list — should trigger the check
        cm.data = {"mapUsers": "key: value", "mapRoles": ""}
        with pytest.raises(click.ClickException, match="mapUsers is not a YAML list"):
            _parse_configmap(cm)

    def test_parse_invalid_map_roles_raises(self):
        from kubernetes import client as k8s_client
        import click
        cm = MagicMock(spec=k8s_client.V1ConfigMap)
        cm.data = {"mapUsers": "", "mapRoles": "key: value"}
        with pytest.raises(click.ClickException, match="mapRoles is not a YAML list"):
            _parse_configmap(cm)

    def test_parse_whitespace_only_values(self):
        from kubernetes import client as k8s_client
        cm = MagicMock(spec=k8s_client.V1ConfigMap)
        cm.data = {"mapUsers": "   ", "mapRoles": "\n"}
        users, roles = _parse_configmap(cm)
        assert users == []
        assert roles == []


# ─── _write_configmap ─────────────────────────────────────────────────────────

class TestWriteConfigmap:
    def test_write_serialises_users_and_roles(self):
        cm = _make_cm(map_users=[], map_roles=[])
        v1 = MagicMock()
        users = [{"userarn": VALID_USER_ARN, "username": "alice", "groups": ["system:masters"]}]
        roles = [{"rolearn": VALID_ROLE_ARN, "username": "my-role", "groups": ["eks-developers"]}]

        _write_configmap(v1, cm, users, roles)

        v1.replace_namespaced_config_map.assert_called_once_with(
            name=CONFIGMAP_NAME, namespace=CONFIGMAP_NAMESPACE, body=cm
        )
        assert yaml.safe_load(cm.data["mapUsers"]) == users
        assert yaml.safe_load(cm.data["mapRoles"]) == roles

    def test_write_empty_lists_produce_empty_string(self):
        cm = _make_cm(map_users=[], map_roles=[])
        v1 = MagicMock()

        _write_configmap(v1, cm, [], [])

        assert cm.data["mapUsers"] == ""
        assert cm.data["mapRoles"] == ""

    def test_write_initialises_none_data(self):
        from kubernetes import client as k8s_client
        cm = MagicMock(spec=k8s_client.V1ConfigMap)
        cm.data = None
        v1 = MagicMock()

        _write_configmap(v1, cm, [], [])

        assert cm.data is not None

    def test_write_api_exception_raises_click_exception(self):
        import click
        cm = _make_cm()
        v1 = MagicMock()
        exc = ApiException(status=500, reason="Internal Server Error")
        v1.replace_namespaced_config_map.side_effect = exc

        with pytest.raises(click.ClickException, match="Failed to update"):
            _write_configmap(v1, cm, [], [])


# ─── _op_add_user ─────────────────────────────────────────────────────────────

class TestOpAddUser:
    def _patch_k8s(self, cm):
        v1, ca_path = _mock_k8s_client(cm)
        return patch.object(am, "_build_k8s_client", return_value=(v1, ca_path)), v1

    def test_add_user_admin_success(self):
        cm = _make_cm(map_users=[], map_roles=[])
        patcher, v1 = self._patch_k8s(cm)
        with patcher:
            _op_add_user(CLUSTER_NAME, VALID_USER_ARN, None, "admin", REGION, dry_run=False)

        v1.replace_namespaced_config_map.assert_called_once()
        written_users = yaml.safe_load(cm.data["mapUsers"])
        assert len(written_users) == 1
        assert written_users[0]["userarn"] == VALID_USER_ARN
        assert written_users[0]["username"] == "alice"
        assert written_users[0]["groups"] == ["system:masters"]

    def test_add_user_developer_success(self):
        cm = _make_cm(map_users=[], map_roles=[])
        patcher, v1 = self._patch_k8s(cm)
        with patcher:
            _op_add_user(CLUSTER_NAME, VALID_USER_ARN, "custom-user", "developer", REGION, dry_run=False)

        written_users = yaml.safe_load(cm.data["mapUsers"])
        assert written_users[0]["username"] == "custom-user"
        assert written_users[0]["groups"] == ["eks-developers"]

    def test_add_user_derives_username_from_arn(self):
        cm = _make_cm(map_users=[], map_roles=[])
        patcher, v1 = self._patch_k8s(cm)
        arn = "arn:aws:iam::123456789012:user/org/team/bob"
        with patcher:
            _op_add_user(CLUSTER_NAME, arn, None, "admin", REGION, dry_run=False)
        written_users = yaml.safe_load(cm.data["mapUsers"])
        assert written_users[0]["username"] == "bob"

    def test_add_user_invalid_arn_raises(self):
        import click
        with pytest.raises(click.ClickException, match="Invalid IAM user ARN"):
            _op_add_user(CLUSTER_NAME, "bad-arn", None, "admin", REGION, dry_run=False)

    def test_add_user_duplicate_raises(self):
        import click
        existing = [{"userarn": VALID_USER_ARN, "username": "alice", "groups": ["system:masters"]}]
        cm = _make_cm(map_users=existing, map_roles=[])
        patcher, _ = self._patch_k8s(cm)
        with patcher:
            with pytest.raises(click.ClickException, match="already present"):
                _op_add_user(CLUSTER_NAME, VALID_USER_ARN, None, "admin", REGION, dry_run=False)

    def test_add_user_dry_run_does_not_write(self):
        cm = _make_cm(map_users=[], map_roles=[])
        patcher, v1 = self._patch_k8s(cm)
        with patcher:
            _op_add_user(CLUSTER_NAME, VALID_USER_ARN, None, "admin", REGION, dry_run=True)
        v1.replace_namespaced_config_map.assert_not_called()

    def test_add_user_cleans_up_ca_file_on_success(self):
        cm = _make_cm(map_users=[], map_roles=[])
        v1, ca_path = _mock_k8s_client(cm)
        with patch.object(am, "_build_k8s_client", return_value=(v1, ca_path)):
            _op_add_user(CLUSTER_NAME, VALID_USER_ARN, None, "admin", REGION, dry_run=False)
        assert not os.path.exists(ca_path)

    def test_add_user_cleans_up_ca_file_on_exception(self):
        import click
        existing = [{"userarn": VALID_USER_ARN, "username": "alice", "groups": []}]
        cm = _make_cm(map_users=existing, map_roles=[])
        v1, ca_path = _mock_k8s_client(cm)
        with patch.object(am, "_build_k8s_client", return_value=(v1, ca_path)):
            with pytest.raises(click.ClickException):
                _op_add_user(CLUSTER_NAME, VALID_USER_ARN, None, "admin", REGION, dry_run=False)
        assert not os.path.exists(ca_path)


# ─── _op_add_role ─────────────────────────────────────────────────────────────

class TestOpAddRole:
    def _patch_k8s(self, cm):
        v1, ca_path = _mock_k8s_client(cm)
        return patch.object(am, "_build_k8s_client", return_value=(v1, ca_path)), v1

    def test_add_role_admin_success(self):
        cm = _make_cm(map_users=[], map_roles=[])
        patcher, v1 = self._patch_k8s(cm)
        with patcher:
            _op_add_role(CLUSTER_NAME, VALID_ROLE_ARN, None, "admin", REGION, dry_run=False)

        written_roles = yaml.safe_load(cm.data["mapRoles"])
        assert written_roles[0]["rolearn"] == VALID_ROLE_ARN
        assert written_roles[0]["username"] == "my-role"
        assert written_roles[0]["groups"] == ["system:masters"]

    def test_add_role_developer_success(self):
        cm = _make_cm(map_users=[], map_roles=[])
        patcher, v1 = self._patch_k8s(cm)
        with patcher:
            _op_add_role(CLUSTER_NAME, VALID_ROLE_ARN, "custom", "developer", REGION, dry_run=False)

        written_roles = yaml.safe_load(cm.data["mapRoles"])
        assert written_roles[0]["groups"] == ["eks-developers"]
        assert written_roles[0]["username"] == "custom"

    def test_add_role_invalid_arn_raises(self):
        import click
        with pytest.raises(click.ClickException, match="Invalid IAM role ARN"):
            _op_add_role(CLUSTER_NAME, "bad-role-arn", None, "admin", REGION, dry_run=False)

    def test_add_role_duplicate_raises(self):
        import click
        existing = [{"rolearn": VALID_ROLE_ARN, "username": "my-role", "groups": []}]
        cm = _make_cm(map_users=[], map_roles=existing)
        patcher, _ = self._patch_k8s(cm)
        with patcher:
            with pytest.raises(click.ClickException, match="already present"):
                _op_add_role(CLUSTER_NAME, VALID_ROLE_ARN, None, "admin", REGION, dry_run=False)

    def test_add_role_dry_run_does_not_write(self):
        cm = _make_cm(map_users=[], map_roles=[])
        patcher, v1 = self._patch_k8s(cm)
        with patcher:
            _op_add_role(CLUSTER_NAME, VALID_ROLE_ARN, None, "admin", REGION, dry_run=True)
        v1.replace_namespaced_config_map.assert_not_called()

    def test_add_role_does_not_touch_map_users(self):
        existing_users = [{"userarn": VALID_USER_ARN, "username": "alice", "groups": ["system:masters"]}]
        cm = _make_cm(map_users=existing_users, map_roles=[])
        patcher, _ = self._patch_k8s(cm)
        with patcher:
            _op_add_role(CLUSTER_NAME, VALID_ROLE_ARN, None, "admin", REGION, dry_run=False)
        written_users = yaml.safe_load(cm.data["mapUsers"])
        assert written_users == existing_users


# ─── _op_remove_user ──────────────────────────────────────────────────────────

class TestOpRemoveUser:
    def _patch_k8s(self, cm):
        v1, ca_path = _mock_k8s_client(cm)
        return patch.object(am, "_build_k8s_client", return_value=(v1, ca_path)), v1

    def test_remove_user_success(self):
        existing = [{"userarn": VALID_USER_ARN, "username": "alice", "groups": ["system:masters"]}]
        cm = _make_cm(map_users=existing, map_roles=[])
        patcher, v1 = self._patch_k8s(cm)
        with patcher:
            _op_remove_user(CLUSTER_NAME, VALID_USER_ARN, REGION, dry_run=False)

        v1.replace_namespaced_config_map.assert_called_once()
        written_users = yaml.safe_load(cm.data["mapUsers"])
        assert written_users == [] or written_users is None or cm.data["mapUsers"] == ""

    def test_remove_user_not_found_raises(self):
        import click
        cm = _make_cm(map_users=[], map_roles=[])
        patcher, _ = self._patch_k8s(cm)
        with patcher:
            with pytest.raises(click.ClickException, match="not found in aws-auth"):
                _op_remove_user(CLUSTER_NAME, VALID_USER_ARN, REGION, dry_run=False)

    def test_remove_user_dry_run_does_not_write(self):
        existing = [{"userarn": VALID_USER_ARN, "username": "alice", "groups": []}]
        cm = _make_cm(map_users=existing, map_roles=[])
        patcher, v1 = self._patch_k8s(cm)
        with patcher:
            _op_remove_user(CLUSTER_NAME, VALID_USER_ARN, REGION, dry_run=True)
        v1.replace_namespaced_config_map.assert_not_called()

    def test_remove_user_leaves_other_users_intact(self):
        other_arn = "arn:aws:iam::123456789012:user/bob"
        existing = [
            {"userarn": VALID_USER_ARN, "username": "alice", "groups": ["system:masters"]},
            {"userarn": other_arn, "username": "bob", "groups": ["eks-developers"]},
        ]
        cm = _make_cm(map_users=existing, map_roles=[])
        patcher, _ = self._patch_k8s(cm)
        with patcher:
            _op_remove_user(CLUSTER_NAME, VALID_USER_ARN, REGION, dry_run=False)

        written_users = yaml.safe_load(cm.data["mapUsers"])
        assert len(written_users) == 1
        assert written_users[0]["userarn"] == other_arn

    def test_remove_user_cleans_up_ca_file(self):
        existing = [{"userarn": VALID_USER_ARN, "username": "alice", "groups": []}]
        cm = _make_cm(map_users=existing, map_roles=[])
        v1, ca_path = _mock_k8s_client(cm)
        with patch.object(am, "_build_k8s_client", return_value=(v1, ca_path)):
            _op_remove_user(CLUSTER_NAME, VALID_USER_ARN, REGION, dry_run=False)
        assert not os.path.exists(ca_path)


# ─── _op_remove_role ──────────────────────────────────────────────────────────

class TestOpRemoveRole:
    def _patch_k8s(self, cm):
        v1, ca_path = _mock_k8s_client(cm)
        return patch.object(am, "_build_k8s_client", return_value=(v1, ca_path)), v1

    def test_remove_role_success(self):
        existing = [{"rolearn": VALID_ROLE_ARN, "username": "my-role", "groups": ["system:masters"]}]
        cm = _make_cm(map_users=[], map_roles=existing)
        patcher, v1 = self._patch_k8s(cm)
        with patcher:
            _op_remove_role(CLUSTER_NAME, VALID_ROLE_ARN, REGION, dry_run=False)
        v1.replace_namespaced_config_map.assert_called_once()
        assert cm.data["mapRoles"] == ""

    def test_remove_role_not_found_raises(self):
        import click
        cm = _make_cm(map_users=[], map_roles=[])
        patcher, _ = self._patch_k8s(cm)
        with patcher:
            with pytest.raises(click.ClickException, match="not found in aws-auth"):
                _op_remove_role(CLUSTER_NAME, VALID_ROLE_ARN, REGION, dry_run=False)

    def test_remove_role_dry_run_does_not_write(self):
        existing = [{"rolearn": VALID_ROLE_ARN, "username": "my-role", "groups": []}]
        cm = _make_cm(map_users=[], map_roles=existing)
        patcher, v1 = self._patch_k8s(cm)
        with patcher:
            _op_remove_role(CLUSTER_NAME, VALID_ROLE_ARN, REGION, dry_run=True)
        v1.replace_namespaced_config_map.assert_not_called()

    def test_remove_role_leaves_other_roles_intact(self):
        other_arn = "arn:aws:iam::123456789012:role/other-role"
        existing = [
            {"rolearn": VALID_ROLE_ARN, "username": "my-role", "groups": []},
            {"rolearn": other_arn, "username": "other-role", "groups": []},
        ]
        cm = _make_cm(map_users=[], map_roles=existing)
        patcher, _ = self._patch_k8s(cm)
        with patcher:
            _op_remove_role(CLUSTER_NAME, VALID_ROLE_ARN, REGION, dry_run=False)

        written_roles = yaml.safe_load(cm.data["mapRoles"])
        assert len(written_roles) == 1
        assert written_roles[0]["rolearn"] == other_arn


# ─── _read_configmap error handling ──────────────────────────────────────────

class TestReadConfigmapErrors:
    def test_404_raises_click_exception(self):
        import click
        v1 = MagicMock()
        exc = ApiException(status=404)
        v1.read_namespaced_config_map.side_effect = exc
        with pytest.raises(click.ClickException, match="aws-auth ConfigMap not found"):
            am._read_configmap(v1)

    def test_403_raises_click_exception(self):
        import click
        v1 = MagicMock()
        exc = ApiException(status=403)
        v1.read_namespaced_config_map.side_effect = exc
        with pytest.raises(click.ClickException, match="Permission denied"):
            am._read_configmap(v1)

    def test_other_api_exception_raises_click_exception(self):
        import click
        v1 = MagicMock()
        exc = ApiException(status=500, reason="Server Error")
        v1.read_namespaced_config_map.side_effect = exc
        with pytest.raises(click.ClickException, match="Kubernetes API error"):
            am._read_configmap(v1)


# ─── _get_cluster_info error handling ─────────────────────────────────────────

class TestGetClusterInfoErrors:
    def _client_error(self, code):
        return ClientError(
            {"Error": {"Code": code, "Message": "msg"}}, "DescribeCluster"
        )

    def test_resource_not_found_raises(self):
        import click
        with patch("boto3.client") as mock_boto:
            mock_boto.return_value.describe_cluster.side_effect = self._client_error(
                "ResourceNotFoundException"
            )
            with pytest.raises(click.ClickException, match="not found in region"):
                am._get_cluster_info(CLUSTER_NAME, REGION)

    def test_other_client_error_raises(self):
        import click
        with patch("boto3.client") as mock_boto:
            mock_boto.return_value.describe_cluster.side_effect = self._client_error(
                "AccessDeniedException"
            )
            with pytest.raises(click.ClickException, match="AWS error"):
                am._get_cluster_info(CLUSTER_NAME, REGION)

    def test_no_credentials_raises(self):
        import click
        with patch("boto3.client") as mock_boto:
            mock_boto.return_value.describe_cluster.side_effect = NoCredentialsError()
            with pytest.raises(click.ClickException, match="credentials"):
                am._get_cluster_info(CLUSTER_NAME, REGION)

    def test_inactive_cluster_raises(self):
        import click
        with patch("boto3.client") as mock_boto:
            mock_boto.return_value.describe_cluster.return_value = {
                "cluster": {
                    "status": "CREATING",
                    "endpoint": "https://example.com",
                    "certificateAuthority": {"data": FAKE_CA_B64},
                }
            }
            with pytest.raises(click.ClickException, match="not ACTIVE"):
                am._get_cluster_info(CLUSTER_NAME, REGION)

    def test_active_cluster_returns_info(self):
        with patch("boto3.client") as mock_boto:
            mock_boto.return_value.describe_cluster.return_value = {
                "cluster": {
                    "status": "ACTIVE",
                    "endpoint": "https://k8s.example.com",
                    "certificateAuthority": {"data": FAKE_CA_B64},
                }
            }
            info = am._get_cluster_info(CLUSTER_NAME, REGION)
        assert info["endpoint"] == "https://k8s.example.com"
        assert info["ca_data"] == FAKE_CA_B64


# ─── _generate_eks_token ──────────────────────────────────────────────────────

class TestGenerateEksToken:
    def test_token_has_k8s_aws_prefix(self):
        fake_url = "https://sts.us-east-1.amazonaws.com/?Action=GetCallerIdentity&X-Amz-Signature=abc"
        with patch("boto3.session.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_sts = MagicMock()
            mock_session.client.return_value = mock_sts
            mock_sts.meta.service_model.service_id = "STS"
            mock_creds = MagicMock()
            mock_session.get_credentials.return_value = mock_creds
            mock_session.events = MagicMock()

            with patch("aws_auth_manager.RequestSigner") as mock_signer_cls:
                mock_signer = MagicMock()
                mock_signer_cls.return_value = mock_signer
                mock_signer.generate_presigned_url.return_value = fake_url

                token = am._generate_eks_token(CLUSTER_NAME, REGION)

        assert token.startswith("k8s-aws-v1.")

    def test_token_is_base64_encoded_url(self):
        fake_url = "https://sts.us-east-1.amazonaws.com/?Action=GetCallerIdentity&X-Amz-Signature=xyz"
        with patch("boto3.session.Session") as mock_session_cls:
            mock_session = MagicMock()
            mock_session_cls.return_value = mock_session
            mock_sts = MagicMock()
            mock_session.client.return_value = mock_sts
            mock_sts.meta.service_model.service_id = "STS"
            mock_session.get_credentials.return_value = MagicMock()
            mock_session.events = MagicMock()

            with patch("aws_auth_manager.RequestSigner") as mock_signer_cls:
                mock_signer = MagicMock()
                mock_signer_cls.return_value = mock_signer
                mock_signer.generate_presigned_url.return_value = fake_url

                token = am._generate_eks_token(CLUSTER_NAME, REGION)

        encoded_part = token[len("k8s-aws-v1."):]
        # Restore padding and decode; should give back the original URL
        padded = encoded_part + "=" * (-len(encoded_part) % 4)
        decoded = base64.urlsafe_b64decode(padded).decode("utf-8")
        assert decoded == fake_url


# ─── CLI integration tests ────────────────────────────────────────────────────

class TestCli:
    """Test CLI commands using Click's test runner; all k8s/AWS calls are mocked."""

    def _patch_ops(self):
        return {
            "add_user":    patch.object(am, "_op_add_user"),
            "add_role":    patch.object(am, "_op_add_role"),
            "remove_user": patch.object(am, "_op_remove_user"),
            "remove_role": patch.object(am, "_op_remove_role"),
            "list_op":     patch.object(am, "_op_list"),
        }

    def test_add_user_command(self):
        runner = CliRunner()
        with patch.object(am, "_op_add_user") as mock_op:
            result = runner.invoke(cli, [
                "add-user",
                "--cluster-name", CLUSTER_NAME,
                "--region", REGION,
                "--user-arn", VALID_USER_ARN,
                "--access", "admin",
            ])
        assert result.exit_code == 0, result.output
        mock_op.assert_called_once_with(
            CLUSTER_NAME, VALID_USER_ARN, None, "admin", REGION, False
        )

    def test_add_user_with_custom_username(self):
        runner = CliRunner()
        with patch.object(am, "_op_add_user") as mock_op:
            result = runner.invoke(cli, [
                "add-user",
                "--cluster-name", CLUSTER_NAME,
                "--region", REGION,
                "--user-arn", VALID_USER_ARN,
                "--username", "custom-alice",
                "--access", "developer",
            ])
        assert result.exit_code == 0, result.output
        mock_op.assert_called_once_with(
            CLUSTER_NAME, VALID_USER_ARN, "custom-alice", "developer", REGION, False
        )

    def test_add_user_dry_run_flag(self):
        runner = CliRunner()
        with patch.object(am, "_op_add_user") as mock_op:
            result = runner.invoke(cli, [
                "add-user",
                "--cluster-name", CLUSTER_NAME,
                "--region", REGION,
                "--user-arn", VALID_USER_ARN,
                "--access", "admin",
                "--dry-run",
            ])
        assert result.exit_code == 0
        _, _, _, _, _, dry_run = mock_op.call_args[0]
        assert dry_run is True

    def test_add_user_missing_required_args(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["add-user", "--cluster-name", CLUSTER_NAME])
        assert result.exit_code != 0
        assert "Missing option" in result.output or "Error" in result.output

    def test_add_user_invalid_access_choice(self):
        runner = CliRunner()
        result = runner.invoke(cli, [
            "add-user",
            "--cluster-name", CLUSTER_NAME,
            "--user-arn", VALID_USER_ARN,
            "--access", "superuser",
        ])
        assert result.exit_code != 0

    def test_add_role_command(self):
        runner = CliRunner()
        with patch.object(am, "_op_add_role") as mock_op:
            result = runner.invoke(cli, [
                "add-role",
                "--cluster-name", CLUSTER_NAME,
                "--region", REGION,
                "--role-arn", VALID_ROLE_ARN,
                "--access", "developer",
            ])
        assert result.exit_code == 0, result.output
        mock_op.assert_called_once_with(
            CLUSTER_NAME, VALID_ROLE_ARN, None, "developer", REGION, False
        )

    def test_remove_user_command(self):
        runner = CliRunner()
        with patch.object(am, "_op_remove_user") as mock_op:
            result = runner.invoke(cli, [
                "remove-user",
                "--cluster-name", CLUSTER_NAME,
                "--region", REGION,
                "--user-arn", VALID_USER_ARN,
            ])
        assert result.exit_code == 0, result.output
        mock_op.assert_called_once_with(CLUSTER_NAME, VALID_USER_ARN, REGION, False)

    def test_remove_role_command(self):
        runner = CliRunner()
        with patch.object(am, "_op_remove_role") as mock_op:
            result = runner.invoke(cli, [
                "remove-role",
                "--cluster-name", CLUSTER_NAME,
                "--region", REGION,
                "--role-arn", VALID_ROLE_ARN,
            ])
        assert result.exit_code == 0, result.output
        mock_op.assert_called_once_with(CLUSTER_NAME, VALID_ROLE_ARN, REGION, False)

    def test_list_command(self):
        runner = CliRunner()
        with patch.object(am, "_op_list") as mock_op:
            result = runner.invoke(cli, [
                "list",
                "--cluster-name", CLUSTER_NAME,
                "--region", REGION,
            ])
        assert result.exit_code == 0, result.output
        mock_op.assert_called_once_with(CLUSTER_NAME, REGION)

    def test_list_missing_cluster_name(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["list"])
        assert result.exit_code != 0

    def test_user_arn_is_stripped_of_whitespace(self):
        runner = CliRunner()
        with patch.object(am, "_op_add_user") as mock_op:
            result = runner.invoke(cli, [
                "add-user",
                "--cluster-name", CLUSTER_NAME,
                "--region", REGION,
                "--user-arn", f"  {VALID_USER_ARN}  ",
                "--access", "admin",
            ])
        assert result.exit_code == 0
        called_arn = mock_op.call_args[0][1]
        assert called_arn == VALID_USER_ARN

    def test_role_arn_is_stripped_of_whitespace(self):
        runner = CliRunner()
        with patch.object(am, "_op_add_role") as mock_op:
            result = runner.invoke(cli, [
                "add-role",
                "--cluster-name", CLUSTER_NAME,
                "--region", REGION,
                "--role-arn", f"  {VALID_ROLE_ARN}  ",
                "--access", "admin",
            ])
        assert result.exit_code == 0
        called_arn = mock_op.call_args[0][1]
        assert called_arn == VALID_ROLE_ARN

    def test_default_region_from_env(self):
        runner = CliRunner()
        with patch.object(am, "_op_list") as mock_op:
            result = runner.invoke(
                cli,
                ["list", "--cluster-name", CLUSTER_NAME],
                env={"AWS_DEFAULT_REGION": "eu-west-1"},
            )
        assert result.exit_code == 0
        _, region = mock_op.call_args[0]
        assert region == "eu-west-1"

    def test_help_flag(self):
        runner = CliRunner()
        result = runner.invoke(cli, ["-h"])
        assert result.exit_code == 0
        assert "aws-auth" in result.output.lower() or "Usage" in result.output


# ─── _op_list output ──────────────────────────────────────────────────────────

class TestOpList:
    def test_list_shows_users_and_roles(self, capsys):
        users = [{"userarn": VALID_USER_ARN, "username": "alice", "groups": ["system:masters"]}]
        roles = [{"rolearn": VALID_ROLE_ARN, "username": "my-role", "groups": ["eks-developers"]}]
        cm = _make_cm(map_users=users, map_roles=roles)
        v1, ca_path = _mock_k8s_client(cm)

        with patch.object(am, "_build_k8s_client", return_value=(v1, ca_path)):
            runner = CliRunner()
            with patch.object(am, "_op_list", wraps=am._op_list):
                # Call directly to capture click.echo output
                from io import StringIO
                import sys
                result = runner.invoke(cli, [
                    "list", "--cluster-name", CLUSTER_NAME, "--region", REGION
                ], catch_exceptions=False)

        # _op_list is mocked away in CLI tests above; call it directly here
        v1b, ca_path_b = _mock_k8s_client(_make_cm(map_users=users, map_roles=roles))
        with patch.object(am, "_build_k8s_client", return_value=(v1b, ca_path_b)):
            runner2 = CliRunner()
            result2 = runner2.invoke(cli, [
                "list", "--cluster-name", CLUSTER_NAME, "--region", REGION,
            ])
        assert VALID_USER_ARN in result2.output
        assert VALID_ROLE_ARN in result2.output
        assert "alice" in result2.output
        assert "my-role" in result2.output

    def test_list_shows_none_when_empty(self):
        cm = _make_cm(map_users=[], map_roles=[])
        v1, ca_path = _mock_k8s_client(cm)
        with patch.object(am, "_build_k8s_client", return_value=(v1, ca_path)):
            runner = CliRunner()
            result = runner.invoke(cli, [
                "list", "--cluster-name", CLUSTER_NAME, "--region", REGION,
            ])
        assert "(none)" in result.output