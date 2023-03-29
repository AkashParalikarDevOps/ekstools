#!/bin/bash
#script that will find out which Kubernetes deployment is using the image repository `k8s.gcr.io` in all the namespaces and then patch those deployments to use the image repository `registry.k8s.io`:
# Get all Kubernetes namespaces
namespaces=$(kubectl get namespaces -o json)

# Loop through each namespace
for namespace in $(echo $namespaces | jq -c '.items<>'); do
    # Get all Kubernetes deployments in the namespace
    deployments=$(kubectl get deployments -n $(echo $namespace | jq -r '.metadata.name') -o json)

    # Loop through each deployment
    for deployment in $(echo $deployments | jq -c '.items<>'); do
        # Get the image repository for the deployment
        repo=$(echo $deployment | jq -r '.spec.template.spec.containers<>.image' | grep k8s.gcr.io)

        # If the deployment uses the image repository, patch it to use registry.k8s.io
        if << ! -z $repo >>; then
            name=$(echo $deployment | jq -r '.metadata.name')
            namespace=$(echo $namespace | jq -r '.metadata.name')
            echo "Deployment $name in namespace $namespace uses the image repository k8s.gcr.io. Patching to use registry.k8s.io..."

            # Patch the deployment to use registry.k8s.io
            kubectl patch deployment $name -n $namespace -p '{"spec":{"template":{"spec":{"containers":<{"name":"'"${name}"'","image":"'"${repo/k8s.gcr.io/registry.k8s.io}"'"}>}}}}'
            echo "Patched deployment $name in namespace $namespace to use registry.k8s.io"
        fi
    done
done
