{{/*
Expand the name of the chart.
*/}}
{{- define "workspace-mcp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
We truncate at 63 chars because some Kubernetes name fields are limited to this (by the DNS naming spec).
If release name contains chart name it will be used as a full name.
*/}}
{{- define "workspace-mcp.fullname" -}}
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
{{- define "workspace-mcp.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "workspace-mcp.labels" -}}
helm.sh/chart: {{ include "workspace-mcp.chart" . }}
{{ include "workspace-mcp.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "workspace-mcp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "workspace-mcp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
The OAuth callback URL the server process will actually use.

Mirrors auth/oauth_config.py's precedence, highest first:
  1. GOOGLE_OAUTH_REDIRECT_URI, verbatim.
  2. WORKSPACE_EXTERNAL_URL + /oauth2callback.
  3. WORKSPACE_MCP_BASE_URI (default http://localhost) + port + /oauth2callback.

Case 2 has to include the value deployment.yaml DERIVES from the first ingress
host, which .Values.env does not carry. Reading .Values.env alone is why NOTES
printed http://localhost:8000/oauth2callback for a release whose pod was handed
WORKSPACE_EXTERNAL_URL=https://<ingress host>.
*/}}
{{- define "workspace-mcp.oauthCallbackUrl" -}}
{{- if .Values.env.GOOGLE_OAUTH_REDIRECT_URI -}}
{{- .Values.env.GOOGLE_OAUTH_REDIRECT_URI -}}
{{- else -}}
{{- $base := .Values.env.WORKSPACE_EXTERNAL_URL | default "" -}}
{{- if and (not $base) .Values.ingress.enabled -}}
{{- $scheme := ternary "https" "http" (gt (len .Values.ingress.tls) 0) -}}
{{- range .Values.ingress.hosts -}}
{{- if and .host (not $base) -}}
{{- $base = printf "%s://%s" $scheme .host -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if not $base -}}
{{- /*
  PORT wins over WORKSPACE_MCP_PORT, matching OAuthConfig.__init__. Reading
  WORKSPACE_MCP_PORT alone printed the wrong port whenever env.PORT was set.

  OAuthConfig swaps that precedence when WORKSPACE_MCP_RESOLVED_PORT=1, and
  this helper deliberately does NOT mirror that branch: the chart always runs
  --transport streamable-http (deployment.yaml), and for any non-stdio
  transport main.py's resolve_callback_port_for_transport POPS
  WORKSPACE_MCP_RESOLVED_PORT before the config is built. Setting it in
  .Values.env therefore cannot affect the running process, so honouring it
  here could only ever print a port the process does not use — the exact
  failure this helper exists to prevent.
*/ -}}
{{- $base = printf "%s:%v"
      (.Values.env.WORKSPACE_MCP_BASE_URI | default "http://localhost")
      (.Values.env.PORT | default .Values.env.WORKSPACE_MCP_PORT | default "8000") -}}
{{- end -}}
{{- printf "%s/oauth2callback" (trimSuffix "/" $base) -}}
{{- end -}}
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "workspace-mcp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "workspace-mcp.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}