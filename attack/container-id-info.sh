crictl ps -q 2>/dev/null | while read container_id; do
    inspect_json=$(crictl inspect "$container_id" 2>/dev/null)

    pod_name=$(echo "$inspect_json" | jq -r 'if type == "array" then .[0].info.config.labels["io.kubernetes.pod.name"] else .info.config.labels["io.kubernetes.pod.name"] end // "unknown"')
    namespace=$(echo "$inspect_json" | jq -r 'if type == "array" then .[0].info.config.labels["io.kubernetes.pod.namespace"] else .info.config.labels["io.kubernetes.pod.namespace"] end // "default"')

    echo "Found pod: $namespace/$pod_name"
    echo "Found namespace: $namespace"
done