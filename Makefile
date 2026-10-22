# Makefile for the beyond-semantic-routing PoC (phase 0).
#
# Image registry : quay.io/eformat
# Deploy target  : the user-mhepburn namespace on the TMM cluster
#                  (KUBECONFIG ~/.kube/config.ibm-tmm)
#
# The image build is LOCAL (podman -> quay), not the in-cluster BuildConfig:
# deploy/20-buildconfig.yaml + 10-imagestream.yaml stay for the later GitOps
# wiring, but the iterate loop here is podman build -> podman push -> oc apply.
#
# undeploy deletes ONLY the label-selected PoC resources -- NEVER the namespace
# itself: user-mhepburn is a pre-existing user sandbox (227d), not ours.

REGISTRY    := quay.io/eformat
PRAXIS_TAG  ?= latest
LLMD_TAG    ?= latest
COLLECTOR_TAG ?= latest

# Unconditional override (not ?=): an ambient KUBECONFIG must never silently
# point the deploy at a different cluster.
KUBECONFIG  := $(HOME)/.kube/config.ibm-tmm
export KUBECONFIG

NAMESPACE    := user-mhepburn
LLMD_SC      := $(HOME)/git/llm-d-semantic-classifier
LABEL        := app.kubernetes.io/part-of=beyond-semantic-routing

PRAXIS_IMAGE := $(REGISTRY)/praxis-beyond-routing:$(PRAXIS_TAG)
LLMD_IMAGE   := $(REGISTRY)/llm-d-sc:$(LLMD_TAG)
LLMD_CAR     := $(REGISTRY)/llm-d-sc-modelcar:$(LLMD_TAG)
COLLECTOR_IMAGE := $(REGISTRY)/beyond-routing-collector:$(COLLECTOR_TAG)

.PHONY: help image image-push llmd-sc-image llmd-sc-image-push llmd-sc-modelcar llmd-sc-modelcar-push collector-image collector-image-push secret deploy status smoke undeploy

help:
	@echo "beyond-semantic-routing PoC — make targets"
	@echo
	@grep -E '^[a-z0-9-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN { FS = ":.*?## " } { printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2 }'
	@echo
	@echo "Variables: PRAXIS_TAG/LLMD_TAG/COLLECTOR_TAG (image tags), NAMESPACE (deploy target),"
	@echo "           MAAS_API_KEY (make secret). KUBECONFIG is pinned in this file."

image: ## Build the praxis-beyond-routing image (praxis v0.7.5 + beyond-routing-filter; long).
	@CTX="$$(hack/stage-build-context.sh)" \
	  && podman build -f "$${CTX}/Containerfile.praxis" -t $(PRAXIS_IMAGE) "$${CTX}"

image-push: ## Push the praxis-beyond-routing image to quay.io/eformat.
	podman push $(PRAXIS_IMAGE)

llmd-sc-image: ## Build the llm-d-sc classifier image (no model baked in; from its own repo).
	podman build -f $(LLMD_SC)/Containerfile -t $(LLMD_IMAGE) $(LLMD_SC)

llmd-sc-image-push: ## Push the llm-d-sc classifier image to quay.io/eformat.
	podman push $(LLMD_IMAGE)

llmd-sc-modelcar: ## Fetch the complexity model (idempotent) + build the ModelCar image.
	$(LLMD_SC)/hack/fetch-model
	@CTX=$$(mktemp -d /tmp/opencode/llmd-modelcar.XXXXXX) \
	  && mkdir -p "$${CTX}/models" \
	  && cp -R $(LLMD_SC)/artifacts/models/complexity/. "$${CTX}/models/" \
	  && chmod -R a+rX "$${CTX}" \
	  && podman build -f $(LLMD_SC)/hack/modelcar/Containerfile -t $(LLMD_CAR) "$${CTX}" \
	  && rm -rf "$${CTX}"

llmd-sc-modelcar-push: ## Push the ModelCar image to quay.io/eformat.
	podman push $(LLMD_CAR)

collector-image: ## Build the decision-collector image (ubi-minimal + python3 stdlib only).
	podman build -f Containerfile.collector -t $(COLLECTOR_IMAGE) collector

collector-image-push: ## Push the decision-collector image to quay.io/eformat.
	podman push $(COLLECTOR_IMAGE)

secret: ## Create the MaaS API key Secret (imperative). Usage: make secret MAAS_API_KEY=sk-oai-...
ifndef MAAS_API_KEY
	$(error MAAS_API_KEY is required: make secret MAAS_API_KEY=sk-oai-...)
endif
	oc -n $(NAMESPACE) create secret generic maas-api-key \
	  --from-literal=key="$(MAAS_API_KEY)" --dry-run=client -o yaml | oc apply -f -

deploy: ## Apply all PoC resources into user-mhepburn (the Secret must exist first).
	oc apply -k deploy

status: ## Rollout status for all three deployments + the resolved Routes.
	oc -n $(NAMESPACE) rollout status deploy/llm-d-sc --timeout=180s
	oc -n $(NAMESPACE) rollout status deploy/praxis --timeout=180s
	oc -n $(NAMESPACE) rollout status deploy/collector --timeout=180s
	@oc -n $(NAMESPACE) get route praxis -o jsonpath='router:    https://{.spec.host}{"\n"}'
	@oc -n $(NAMESPACE) get route collector -o jsonpath='flow-view: https://{.spec.host}{"\n"}'

smoke: ## Resolve the Route + key from the cluster and run the smoke test.
	@PRAXIS_ROUTE=$$(oc -n $(NAMESPACE) get route praxis -o jsonpath='{.spec.host}') \
	  && KEY=$$(oc -n $(NAMESPACE) get secret maas-api-key -o jsonpath='{.data.key}' | base64 -d) \
	  && PRAXIS_ROUTE="$$PRAXIS_ROUTE" PRAXIS_NAMESPACE=$(NAMESPACE) MAAS_API_KEY="$$KEY" \
	     bash deploy/smoke-test.sh

undeploy: ## Delete ONLY the label-selected PoC resources -- NEVER the namespace itself.
	oc -n $(NAMESPACE) delete deployment,service,route,buildconfig,imagestream,configmap \
	  -l '$(LABEL)' --ignore-not-found
