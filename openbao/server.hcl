# DEV ONLY. Single-node OpenBao with raft storage on the named volume openbao-data (survives Stop/Start and container
# recreate; `docker compose down -v`, `make reset` and `make clean` delete it). Plain http, one unseal share, keys kept by
# openbao/unseal.sh in .secrets/openbao: none of this is a production setup.
# Raft with one node, because OpenBao 2.7 no longer has the "file" storage backend ("unknown storage type file"). The node
# bootstraps itself at `operator init`; the cluster address is loopback only and never published.
ui = false
api_addr = "http://openbao:8200"
cluster_addr = "http://127.0.0.1:8201"

storage "raft" {
  path    = "/openbao/file"
  node_id = "dev-1"
}

listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}
