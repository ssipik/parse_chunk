variable "image_repo" {
  type    = string
  default = "getregistry.azurecr.io/rag_parse_chunk"
}
variable "image_tag" {
  type = string
}

# Stage 4 (PARSE+CHUNK). Registered by bitbucket-pipelines.yml. One dispatch per plan
# batch; Airflow's NomadMultiDispatchOperator keeps at most N of them running:
#   nomad job dispatch -meta batch_id=<id> rag-parse-chunk-template
# The run's open batches come from Postgres (plan's output never reaches Airflow).
job "rag-parse-chunk-template" {
  datacenters = ["dc1"]
  type        = "batch"

  parameterized {
    # A plan batch id; the stage re-runs only its pending/error documents.
    meta_required = ["batch_id"]
  }

  group "parse_chunk" {
    count = 1

    restart {
      attempts = 0
      mode     = "fail"
    }
    # Retries are Airflow's job, not Nomad's.
    reschedule {
      attempts  = 0
      unlimited = false
    }

    task "parse_chunk" {
      driver = "docker"

      config {
        image   = "${var.image_repo}:${var.image_tag}"
        command = "python"
        args    = ["-m", "rag_parse_chunk", "--batch-id", "${NOMAD_META_batch_id}"]
        # local_pdfs in sources.yaml reads /mnt/zdrive/RAG-Data/ETFs
        volumes = ["/mnt/zdrive/:/mnt/zdrive"]
      }

      vault {}

      # Vault secrets + the rag-postgres address from Consul, rendered as env vars
      # (same pattern as rag-postgres.hcl), so the code only ever reads env vars.
      # Confluence credentials: pages are fetched through the Confluence API.
      template {
        data        = <<EOT
{{- with secret "kv/data/secret/cluster/rag" }}
{{- $user := .Data.data.POSTGRES_USER }}{{ $pw := .Data.data.POSTGRES_PASSWORD }}
{{- range service "rag-postgres" }}
RAG_PG_DSN=postgresql+psycopg://{{ $user }}:{{ $pw | urlquery }}@{{ .Address }}:{{ .Port }}/rag_ingestion
{{- end }}
RAG_CONFLUENCE_EMAIL={{ .Data.data.RAG_CONFLUENCE_EMAIL }}
RAG_CONFLUENCE_API_TOKEN={{ .Data.data.RAG_CONFLUENCE_API_TOKEN }}
{{- end }}
EOT
        destination = "secrets/rag.env"
        env         = true
        # A batch run must not be restarted by a secret rotation mid-run.
        change_mode = "noop"
      }

      # ARCHITECTURE.md resource plan: 2 cores / 4 GB per parse batch (the OCR peak;
      # digital-native PDFs need about half). ~2 GHz per core on this cluster.
      resources {
        cpu    = 4000
        memory = 4096
      }
    }
  }
}
