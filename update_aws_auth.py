import boto3
import yaml
import click

@click.command()
@click.option('--cluster-name', required=True, help='The name of the EKS cluster.')
@click.option('--user-arn', required=True, help='The ARN of the IAM user to add to the aws-auth ConfigMap.')
@click.option('--admin', is_flag=True, help='Add the user with administrative permissions.')
@click.option('--read-only', is_flag=True, help='Add the user with read-only permissions.')
def update_aws_auth(cluster_name, user_arn, admin, read_only):
    """Update the aws-auth ConfigMap for an EKS cluster with an IAM user."""
    eks = boto3.client('eks')
    try:
        response = eks.describe_cluster(name=cluster_name)
        status = response['cluster']['status']
        if status != 'ACTIVE':
            raise Exception(f'Cluster is not active: {status}')
        config_map_name = 'aws-auth'
        config_map_namespace = 'kube-system'
        config_map = eks.describe_config_map(name=config_map_name, namespace=config_map_namespace)
        data = yaml.safe_load(config_map['data']['mapRoles'])
        for role in data['rolearn']:
            if role == user_arn:
                raise Exception('User is already present in the aws-auth ConfigMap')
        if admin:
            access = 'admin'
        elif read_only:
            access = 'read-only'
        else:
            access = 'arn:aws:iam::{}:user/{}'.format(user_arn.split(':')[4], user_arn.split('/')[-1])
        data['rolearn'].append(user_arn)
        data['username'].append(access)
        data_yaml = yaml.dump(data, default_flow_style=False)
        eks.update_config_map(name=config_map_name, namespace=config_map_namespace, data={'mapRoles': data_yaml})
        click.echo(f'User {user_arn} added to aws-auth ConfigMap with {access} access')
    except Exception as e:
        click.echo(f'Error: {str(e)}', err=True)
        raise SystemExit(1)

if __name__ == '__main__':
    update_aws_auth()
