{{- define "permissions-ui.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "permissions-ui.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else if contains (include "permissions-ui.name" .) .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name (include "permissions-ui.name" .) | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}

{{- /* The web pods only: the expiry job's pods must not receive Service traffic. */}}
{{- define "permissions-ui.selectorLabels" -}}
app.kubernetes.io/name: {{ include "permissions-ui.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/component: web
{{- end }}

{{- define "permissions-ui.labels" -}}
app.kubernetes.io/name: {{ include "permissions-ui.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "permissions-ui.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "permissions-ui.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- required "serviceAccount.name is required when serviceAccount.create is false" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{- define "permissions-ui.image" -}}
{{- $tag := .Values.image.tag | default .Chart.AppVersion }}
{{- if .Values.image.digest }}
{{- printf "%s:%s@%s" .Values.image.repository $tag .Values.image.digest }}
{{- else }}
{{- printf "%s:%s" .Values.image.repository $tag }}
{{- end }}
{{- end }}

{{- define "permissions-ui.sessionSecret" -}}
{{- default (printf "%s-session" (include "permissions-ui.fullname" .)) .Values.session.existingSecret }}
{{- end }}

{{- define "permissions-ui.host" -}}
{{- default (regexReplaceAll "^https?://([^/:]+).*$" .Values.config.publicOrigin "${1}") .Values.ingress.host }}
{{- end }}

{{- /* Fails the render on a missing required value, instead of installing something half-configured. */}}
{{- define "permissions-ui.validate" -}}
{{- $_ := required "config.publicOrigin is required, such as https://access.example.com" .Values.config.publicOrigin }}
{{- $_ := required "azure.tenantId is required" .Values.azure.tenantId }}
{{- $_ := required "azure.clientId is required" .Values.azure.clientId }}
{{- $_ := required "catalog.repository is required: owner/name of the catalog repository" .Values.catalog.repository }}
{{- if and (not .Values.azure.workloadIdentity.enabled) (not .Values.azure.clientSecret.existingSecret) }}
{{- fail "set azure.workloadIdentity.enabled, or azure.clientSecret.existingSecret" }}
{{- end }}
{{- if not (or (and .Values.catalog.app.id .Values.catalog.app.privateKey.existingSecret) .Values.catalog.token.existingSecret) }}
{{- fail "set catalog.app.id with catalog.app.privateKey.existingSecret (preferred), or catalog.token.existingSecret" }}
{{- end }}
{{- if not (has .Values.config.fulfilment (list "pull-request" "graph")) }}
{{- fail "config.fulfilment takes pull-request or graph" }}
{{- end }}
{{- if not (has .Values.config.directory (list "app" "delegated")) }}
{{- fail "config.directory takes app or delegated" }}
{{- end }}
{{- if and (eq .Values.config.directory "delegated") (eq .Values.config.fulfilment "graph") }}
{{- fail "config.fulfilment graph changes groups as the app, so it needs config.directory app" }}
{{- end }}
{{- end }}

{{- define "permissions-ui.env" -}}
- name: AZURE_TENANT_ID
  value: {{ .Values.azure.tenantId | quote }}
- name: AZURE_CLIENT_ID
  value: {{ .Values.azure.clientId | quote }}
{{- with .Values.azure.authorityHost }}
- name: AZURE_AUTHORITY_HOST
  value: {{ . | quote }}
{{- end }}
{{- if .Values.azure.workloadIdentity.enabled }}
- name: AZURE_FEDERATED_TOKEN_FILE
  value: /var/run/secrets/azure/tokens/azure-identity-token
{{- else }}
# Mounted files rather than env vars: an environment variable is readable in /proc/<pid>/environ
# and inherited by child processes.
- name: AZURE_CLIENT_SECRET_FILE
  value: /etc/permissions-ui/client/{{ .Values.azure.clientSecret.key }}
{{- end }}
# The tag deployed, not the one baked in: a release promotes its pull request's image as it is,
# so the image itself still says pr-<N>.
- name: APP_VERSION
  value: {{ .Values.image.tag | default .Chart.AppVersion | quote }}
- name: PERMISSIONS_UI_PUBLIC_ORIGIN
  value: {{ .Values.config.publicOrigin | quote }}
- name: PERMISSIONS_UI_SESSION_HOURS
  value: {{ .Values.config.sessionHours | quote }}
- name: PERMISSIONS_UI_SESSION_KEY_FILE
  value: /etc/permissions-ui/session/{{ .Values.session.key }}
- name: PERMISSIONS_UI_FULFILMENT
  value: {{ .Values.config.fulfilment | quote }}
- name: PERMISSIONS_UI_DIRECTORY
  value: {{ .Values.config.directory | quote }}
- name: PERMISSIONS_UI_MERGE
  value: {{ .Values.config.merge | quote }}
{{- with .Values.config.groupPrefixes }}
- name: PERMISSIONS_UI_GROUP_PREFIXES
  value: {{ join "," . | quote }}
{{- end }}
- name: PERMISSIONS_UI_ADMIN_ROLE
  value: {{ .Values.config.adminRole | quote }}
- name: PERMISSIONS_UI_AUDITOR_ROLE
  value: {{ .Values.config.auditorRole | quote }}
- name: PERMISSIONS_UI_GITHUB_REPO
  value: {{ .Values.catalog.repository | quote }}
- name: PERMISSIONS_UI_GITHUB_BRANCH
  value: {{ .Values.catalog.branch | quote }}
- name: PERMISSIONS_UI_CATALOG_PATH
  value: {{ .Values.catalog.path | quote }}
- name: PERMISSIONS_UI_GITHUB_API
  value: {{ .Values.catalog.apiUrl | quote }}
{{- with .Values.catalog.bot }}
- name: PERMISSIONS_UI_GITHUB_BOT
  value: {{ . | quote }}
{{- end }}
{{- if .Values.catalog.app.id }}
- name: PERMISSIONS_UI_GITHUB_APP_ID
  value: {{ .Values.catalog.app.id | quote }}
- name: PERMISSIONS_UI_GITHUB_APP_PRIVATE_KEY_FILE
  value: /etc/permissions-ui/github/{{ .Values.catalog.app.privateKey.key }}
{{- else }}
- name: PERMISSIONS_UI_GITHUB_TOKEN_FILE
  value: /etc/permissions-ui/github/{{ .Values.catalog.token.key }}
{{- end }}
{{- if .Values.notifications.teams.existingSecret }}
- name: PERMISSIONS_UI_TEAMS_WEBHOOK_FILE
  value: /etc/permissions-ui/teams/{{ .Values.notifications.teams.key }}
{{- end }}
{{- range $key, $env := dict "accent" "ACCENT" "accentText" "ACCENT_TEXT" "accentDark" "ACCENT_DARK" "accentTextDark" "ACCENT_TEXT_DARK" }}
{{- with index $.Values.branding $key }}
- name: PERMISSIONS_UI_BRAND_{{ $env }}
  value: {{ . | quote }}
{{- end }}
{{- end }}
{{- with .Values.extraEnv }}
{{ toYaml . }}
{{- end }}
{{- end }}

{{- define "permissions-ui.volumeMounts" -}}
- name: tmp
  mountPath: /tmp
- name: session
  mountPath: /etc/permissions-ui/session
  readOnly: true
- name: github
  mountPath: /etc/permissions-ui/github
  readOnly: true
{{- if .Values.azure.workloadIdentity.enabled }}
- name: azure-identity-token
  mountPath: /var/run/secrets/azure/tokens
  readOnly: true
{{- else }}
- name: client-secret
  mountPath: /etc/permissions-ui/client
  readOnly: true
{{- end }}
{{- if .Values.notifications.teams.existingSecret }}
- name: teams
  mountPath: /etc/permissions-ui/teams
  readOnly: true
{{- end }}
{{- end }}

{{- define "permissions-ui.volumes" -}}
- name: tmp
  emptyDir:
    sizeLimit: 16Mi
- name: session
  secret:
    secretName: {{ include "permissions-ui.sessionSecret" . }}
    defaultMode: 0440
    items:
      - key: {{ .Values.session.key }}
        path: {{ .Values.session.key }}
{{- $github := ternary .Values.catalog.app.privateKey .Values.catalog.token (not (empty .Values.catalog.app.id)) }}
- name: github
  secret:
    secretName: {{ $github.existingSecret }}
    defaultMode: 0440
    items:
      - key: {{ $github.key }}
        path: {{ $github.key }}
{{- if .Values.azure.workloadIdentity.enabled }}
# The service-account token Entra ID exchanges through the federated credential. The kubelet
# renews it in place before it expires; the app reads the file on every use.
- name: azure-identity-token
  projected:
    defaultMode: 0440
    sources:
      - serviceAccountToken:
          path: azure-identity-token
          audience: {{ .Values.azure.workloadIdentity.audience }}
          expirationSeconds: 3600
{{- else }}
- name: client-secret
  secret:
    secretName: {{ .Values.azure.clientSecret.existingSecret }}
    defaultMode: 0440
    items:
      - key: {{ .Values.azure.clientSecret.key }}
        path: {{ .Values.azure.clientSecret.key }}
{{- end }}
{{- with .Values.notifications.teams }}
{{- if .existingSecret }}
- name: teams
  secret:
    secretName: {{ .existingSecret }}
    defaultMode: 0440
    items:
      - key: {{ .key }}
        path: {{ .key }}
{{- end }}
{{- end }}
{{- end }}

{{- define "permissions-ui.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
fsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end }}

{{- define "permissions-ui.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: ["ALL"]
{{- end }}
