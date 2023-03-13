import boto3
import click

@click.command()
@click.option('--cluster-name', required=True, help='The name of the EKS cluster.')
@click.option('--role-arn', required=True, help='The ARN of the IAM role to add to the aws-auth ConfigMap.')
@click.option('--username', required=True, help='The username to use in the aws-auth ConfigMap.')
@click.option('--read-only', is_flag=True, default=False, help='Add role as read-only.')
@click.option('--admin', is_flag=True, default=False, help='Add role as admin.')

def add_iam_role_to_aws_auth(cluster_name, role_arn, username, read_only, admin):
    """Add IAM role to aws-auth ConfigMap in EKS cluster."""
    
    eks_client = boto3.client('eks')
    cluster = eks_client.describe_cluster(name=cluster_name)['cluster']
    role = f"system:{'readonly' if read_only else ('admin' if admin else 'node')}"

    # Define aws-auth ConfigMap patch
    configmap_patch = {
        'apiVersion': 'v1',
        'data': {
            'mapRoles': f"-\n  rolearn: {role_arn}\n  username: {username}\n  groups:\n    - {role}"
        },
        'kind': 'ConfigMap',
        'metadata': {
            'name': 'aws-auth',
            'namespace': 'kube-system'
        }
    }

    # Patch the aws-auth ConfigMap
    response = eks_client.patch_cluster_config(name=cluster_name, 
                                                resources=['configmaps'], 
                                                patch=configmap_patch)

    if response['ResponseMetadata']['HTTPStatusCode'] == 200:
        click.echo(f"IAM role {role_arn} has been added to the aws-auth ConfigMap in cluster {cluster_name} as {role}.")
    else:
        click.echo(f"Failed to add IAM role {role_arn} to the aws-auth ConfigMap in cluster {cluster_name}.")

if __name__ == '__main__':
    add_iam_role_to_aws_auth()
