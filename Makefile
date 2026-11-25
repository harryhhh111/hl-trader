RUST_IMAGE ?= rust:1.94-slim-bookworm
WEB_TEST_IMAGE ?= hl-app-web:local
E2E_IMAGE ?= hl-app-e2e-test:local
RUST_RUN = docker run --rm -e CARGO_HOME=/work/.runtime/cargo -v hl-trader-rustup:/usr/local/rustup -v $(CURDIR):/work -w /work $(RUST_IMAGE)

.PHONY: test-fast test-rust test-backend test-frontend test-db test-e2e test-gateway test-image verify test-release-tools research-plan research-verify research-sync-plan research-sync

test-fast: test-rust test-backend test-frontend test-release-tools

test-release-tools:
	python3 -m unittest discover -s deploy/tests -v

research-plan:
	python3 deploy/research_release.py plan

research-verify:
	python3 deploy/research_release.py verify

research-sync-plan:
	python3 deploy/sync_research_reports.py plan

# Content publication requires explicit authorization; no service restart.
research-sync:
	python3 deploy/sync_research_reports.py sync --apply

test-image:
	docker image inspect $(WEB_TEST_IMAGE) >/dev/null 2>&1 || docker build --target web -t $(WEB_TEST_IMAGE) .

test-rust:
	# Shared target caches may contain this crate from an isolated release tree.
	# Rebuild the dev crate while preserving dependency and release caches.
	$(RUST_RUN) sh -c 'rustup component add rustfmt clippy && cargo clean --package hl-trader --profile dev && cargo fmt --all --check && cargo clippy --all-targets -- -D warnings && cargo test'

test-backend: test-image
	docker run --rm -v $(CURDIR):/work -w /work -e PYTHONPATH=/work/backend $(WEB_TEST_IMAGE) python -m pytest backend/tests -m 'not db and not live' -q

test-frontend:
	cd frontend && npm run test:unit && npm run build

test-db: test-image
	sh deploy/docker/test-db.sh

test-e2e:
	docker build -f deploy/docker/Dockerfile.e2e -t $(E2E_IMAGE) .
	docker run --rm -v $(CURDIR):/src:ro $(E2E_IMAGE)

test-gateway:
	sh deploy/docker/test-gateway-resolution.sh

verify: test-fast test-db test-e2e test-gateway
