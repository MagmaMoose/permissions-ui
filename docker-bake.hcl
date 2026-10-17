# Diatreme sets these for a release: VERSION (pr-<N> on a pull request, the release tag
# after a merge), REGISTRY and IMAGE_NAME (<owner>/<repository>).
variable "VERSION" { default = "latest" }
variable "REGISTRY" { default = "ghcr.io" }
variable "IMAGE_NAME" { default = "magmamoose/permissions-ui" }
variable "PLATFORMS" { default = "linux/amd64,linux/arm64" }

group "default" {
  targets = ["app"]
}

target "app" {
  context    = "."
  dockerfile = "Dockerfile"
  platforms  = split(",", PLATFORMS)
  tags       = ["${REGISTRY}/${IMAGE_NAME}:${VERSION}"]
  args = {
    VERSION = "${VERSION}"
  }
  labels = {
    "org.opencontainers.image.source"      = "https://github.com/MagmaMoose/permissions-ui"
    "org.opencontainers.image.description" = "See and manage who has access to what, with requests, approvals and Git as the record"
    "org.opencontainers.image.licenses"    = "Apache-2.0"
  }
}

# Local single-arch build that loads into Docker: docker buildx bake app-local
target "app-local" {
  inherits  = ["app"]
  platforms = []
  tags      = ["permissions-ui:dev"]
  output    = ["type=docker"]
}
