# Auth via a named profile in ~/.databrickscfg (never hardcode tokens).
# The reference sandbox for this repo is the "fevm-west" profile.
provider "databricks" {
  profile = var.databricks_profile
}
