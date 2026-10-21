{{/*
Expand the name of the chart.
*/}}
{{- define "poll-app.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "poll-app.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "poll-app.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "poll-app.labels" -}}
helm.sh/chart: {{ include "poll-app.chart" . }}
{{ include "poll-app.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "poll-app.selectorLabels" -}}
app.kubernetes.io/name: {{ include "poll-app.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Resource names
*/}}
{{- define "poll-app.poll.fullname" -}}
{{- printf "%s-poll" (include "poll-app.fullname" . | trunc 58 | trimSuffix "-") }}
{{- end }}

{{- define "poll-app.redis.fullname" -}}
{{- printf "%s-redis" (include "poll-app.fullname" . | trunc 58 | trimSuffix "-") }}
{{- end }}

{{- define "poll-app.postgres.fullname" -}}
{{- printf "%s-postgres" (include "poll-app.fullname" . | trunc 58 | trimSuffix "-") }}
{{- end }}

{{- define "poll-app.result.fullname" -}}
{{- printf "%s-result" (include "poll-app.fullname" . | trunc 58 | trimSuffix "-") }}
{{- end }}

{{- define "poll-app.worker.fullname" -}}
{{- printf "%s-worker" (include "poll-app.fullname" . | trunc 58 | trimSuffix "-") }}
{{- end }}
