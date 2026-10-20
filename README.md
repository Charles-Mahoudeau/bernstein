# Bernstein

## Deploy to Azure (WIP)

### Install `traefik`

The deployment needs Traefik to be installed in the cluster, use the following commands to install it:
```shell
helm repo add traefik https://traefik.github.io/charts
helm install traefik traefik/traefik -n traefik --create-namespace
kubectl get svc -n traefik traefik   # EXTERNAL-IP is the public address
```

### Deploy

Use these commands to deploy the application:
```shell
# Don't forget to load your .env file
# Fish: bass set -a \; source .env \; set +a
kubectl create namespace $NAMESPACE
kubectl -n $NAMESPACE create secret generic postgres-secret --from-literal=POSTGRES_PASSWORD=$POSTGRES_PASSWORD
kubectl -n $NAMESPACE create secret generic redis-secret --from-literal=REDIS_PASSWORD=$REDIS_PASSWORD
helm upgrade --install poll-app ./charts/poll-app \
  --namespace $NAMESPACE \
  --set registry.url="$AZURE_ACR_NAME.azurecr.io" \
  --set poll.image.tag="test"
```
