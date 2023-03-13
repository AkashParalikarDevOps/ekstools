# ekstools
this repo consist of different command line tools which is customise according to the day to day use case 

update_aws_auth.py

Save the script code in a file, for example, update_aws_auth.py.

Install the required dependencies by running the following command in your terminal:

1 Copy code

2 pip install boto3 click
Set up your AWS credentials by either exporting them as environment variables or using the aws configure command. The AWS CLI documentation has more information on how to do this: https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html

3 Run the script with the required arguments. Here's an example:
python update_aws_auth.py --cluster-name my-eks-cluster --user-arn arn:aws:iam::123456789012:user/my-user --read-only
This command will add the my-user IAM user with the specified ARN to the aws-auth ConfigMap for the my-eks-cluster EKS cluster with read-only permissions. If the user is already present in the ConfigMap, the script will display an error message and exit.

You can also run python update_aws_auth.py --help to see all available options and their descriptions.

4 for admin user 

python update_aws_auth.py --cluster-name my-eks-cluster --user-arn arn:aws:iam::123456789012:user/my-user --admin

