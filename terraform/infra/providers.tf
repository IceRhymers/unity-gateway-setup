# Auth via a named profile in ~/.databrickscfg (never hardcode tokens).
# This repo targets one profile NAME on every machine, "ai_dev_tools". The name is
# fixed so a local test and an MDM rollout share one auth path. The workspace it
# points at is the operator's own, so the host is not pinned here.
provider "databricks" {
  profile = var.databricks_profile
}
