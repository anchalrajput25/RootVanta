{{- define "ia.fullname" -}}
{{- if contains .Chart.Name .Release.Name -}}
{{- .Release.Name | trunc 50 | trimSuffix "-" -}}
{{- else -}}
{{- printf "%s-%s" .Release.Name .Chart.Name | trunc 50 | trimSuffix "-" -}}
{{- end -}}
{{- end -}}

{{- define "ia.labels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}

{{- define "ia.selector" -}}
app.kubernetes.io/name: {{ .root.Chart.Name }}
app.kubernetes.io/instance: {{ .root.Release.Name }}
app.kubernetes.io/component: {{ .component }}
{{- end -}}

{{- define "ia.secretName" -}}
{{- .Values.secrets.existingSecret | default (printf "%s-secrets" (include "ia.fullname" .)) -}}
{{- end -}}

{{/* Environment shared by the API and the workers. */}}
{{- define "ia.env" -}}
- name: INCIDENT_AGENT_DATABASE_URL
  valueFrom: {secretKeyRef: {name: {{ include "ia.secretName" . }}, key: database-url}}
- name: INCIDENT_AGENT_JWT_SECRET
  valueFrom: {secretKeyRef: {name: {{ include "ia.secretName" . }}, key: jwt-secret}}
- name: INCIDENT_AGENT_SECRET_KEY
  valueFrom: {secretKeyRef: {name: {{ include "ia.secretName" . }}, key: secret-key}}
- name: INCIDENT_AGENT_PUBLIC_URL
  value: {{ .Values.publicUrl | quote }}
- name: INCIDENT_AGENT_BLOCK_PRIVATE_URLS
  value: {{ ternary "1" "0" .Values.blockPrivateUrls | quote }}
- name: INCIDENT_AGENT_USE_AI
  value: {{ ternary "1" "0" .Values.ai.enabled | quote }}
{{- if .Values.ai.enabled }}
- name: ANTHROPIC_API_KEY
  valueFrom: {secretKeyRef: {name: {{ include "ia.secretName" . }}, key: anthropic-api-key}}
{{- end }}
- name: PYTHONDONTWRITEBYTECODE
  value: "1"
{{- end -}}

{{- define "ia.podExtras" -}}
{{- with .Values.imagePullSecrets }}
imagePullSecrets: {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.nodeSelector }}
nodeSelector: {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.tolerations }}
tolerations: {{- toYaml . | nindent 2 }}
{{- end }}
{{- with .Values.affinity }}
affinity: {{- toYaml . | nindent 2 }}
{{- end }}
{{- end -}}
