{{/* Standard labels. Call with (dict "name" "<component>" "root" .). */}}
{{- define "store.labels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/part-of: store
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ .root.Chart.Name }}-{{ .root.Chart.Version }}
{{- end -}}

{{/* storageClassName line, only when a class is set ("" = the cluster default). */}}
{{- define "store.storageClass" -}}
{{- if .Values.storage.className -}}
storageClassName: {{ .Values.storage.className }}
{{- end -}}
{{- end -}}
