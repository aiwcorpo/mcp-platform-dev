# OpenBao 2.x enables audit devices only declaratively. Values in the log are HMAC-ed.
audit "file" "stdout" {
  options {
    file_path = "stdout"
  }
}
