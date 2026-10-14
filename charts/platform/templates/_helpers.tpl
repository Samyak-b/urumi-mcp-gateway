{{/* Standard labels. Call with (dict "name" "<component>" "root" .). */}}
{{- define "platform.labels" -}}
app.kubernetes.io/name: {{ .name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/part-of: platform
app.kubernetes.io/managed-by: {{ .root.Release.Service }}
helm.sh/chart: {{ .root.Chart.Name }}-{{ .root.Chart.Version }}
{{- end -}}

{{/* The seed servers as the JSON the gateway reads, with each credential filled in from .Values.secrets. */}}
{{- define "platform.seedServers" -}}
{{- $out := list -}}
{{- range .Values.seedServers -}}
{{- $s := dict "slug" .slug "name" .name "url" .url "auth_type" (.authType | default "none") -}}
{{- if .authHeader }}{{- $_ := set $s "auth_header" .authHeader -}}{{- end -}}
{{- if .credentialSecretKey -}}
{{- $cred := index $.Values.secrets .credentialSecretKey -}}
{{- $_ := set $s "credential" (required (printf "secrets.%s is required for seed server %s (run make secrets)" .credentialSecretKey .slug) $cred) -}}
{{- end -}}
{{- $out = append $out $s -}}
{{- end -}}
{{- toJson $out -}}
{{- end -}}

{{/* Container security settings shared by our own images. */}}
{{- define "platform.restricted" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
capabilities: { drop: ["ALL"] }
{{- end -}}
