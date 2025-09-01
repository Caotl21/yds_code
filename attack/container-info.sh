crictl ps -a 2>/dev/null | tail -n +2 | while read -r line; do
    container_id=$(echo "$line" | awk '{print $1}')
    pod_name=$(echo "$line" | awk '{print $NF}')  # POD ID 是最后一列

    inspect_json=$(crictl inspect "$container_id" 2>/dev/null)

    pod_name=$(echo "$inspect_json" | jq -r 'if type == "array" then .[0].info.config.labels["io.kubernetes.pod.name"] else .info.config.labels["io.kubernetes.pod.name"] end // "unknown"')
    namespace=$(echo "$inspect_json" | jq -r 'if type == "array" then .[0].info.config.labels["io.kubernetes.pod.namespace"] else .info.config.labels["io.kubernetes.pod.namespace"] end // "default"')

    echo "Found pod: $namespace/$pod_name"
    echo "Found namespace: $namespace"


    echo "Container ID: $container_id"
    echo "Image: $image"
    echo "State: $state"
    echo "Name: $name"
    echo "POD ID: $pod_id"
    echo "--------"

    # 示例：你可以在这里加上 `crictl inspect "$container_id"` 或其他处理逻辑
done
