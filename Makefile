.PHONY: help build up down logs login sync sync-full probe classify reclassify shell \
        unstick reset status doctor prune-comments prune-comments-apply delete deleted

help:
	@echo "make build     - 이미지 빌드"
	@echo "make login     - 최초 1회 Threads 로그인 (브라우저: http://localhost:6080/vnc.html?autoconnect=1&resize=scale)"
	@echo "make up        - 웹 + 스케줄러 실행 (http://localhost:8080)"
	@echo "make down      - 중지"
	@echo "make logs      - 로그 보기"
	@echo "make sync      - 지금 즉시 수집 + 분류 (새로 저장한 글만 빠르게)"
	@echo "make sync-full - 저장됨 목록을 끝까지 훑어서 수집 (누락 확인용)"
	@echo "make doctor    - 새 코드가 적용됐는지 · 왜 느린지 한 번에 진단"
	@echo "make status    - 다음 동기화가 전체 훑기인지 증분인지 확인"
	@echo "make probe     - 저장 없이 '몇 개까지 긁히는지'만 확인 (스크롤 진단)"
	@echo "make classify  - 미분류 글만 분류"
	@echo "make reclassify- 카테고리 체계 재구성 후 전체 재분류"
	@echo "make prune-comments       - 이미 저장된 것 중 '댓글'을 찾아서 보여주기 (삭제 안 함)"
	@echo "make prune-comments-apply - 위에서 확인한 댓글을 실제로 삭제"
	@echo "make deleted   - 로컬에서 지운 글 목록 (복원: deleted --restore <id|all> 후 sync-full)"
	@echo "make delete ID=<글id>     - 글을 로컬에서만 삭제 (Threads 저장 목록은 유지)"
	@echo "make unstick   - '실행 중'에서 멈춘 작업 정리 (새 작업이 안 걸릴 때)"
	@echo "make reset     - 데이터베이스 초기화 (미디어/세션은 유지)"

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f --tail=100

login:
	docker compose --profile login up login

sync:
	docker compose exec worker python -m app.pipeline sync

sync-full:
	docker compose exec worker python -m app.pipeline sync --full

probe:
	docker compose exec worker python -m app.pipeline probe

classify:
	docker compose exec worker python -m app.pipeline classify

reclassify:
	docker compose exec worker python -m app.pipeline reclassify

shell:
	docker compose exec worker bash

prune-comments:
	docker compose exec worker python -m app.pipeline prune-comments

prune-comments-apply:
	docker compose exec worker python -m app.pipeline prune-comments --apply

doctor:
	docker compose exec worker python -m app.pipeline doctor

status:
	docker compose exec worker python -m app.pipeline status

deleted:
	docker compose exec worker python -m app.pipeline deleted

delete:
	@test -n "$(ID)" || (echo "사용법: make delete ID=<글id>"; exit 1)
	docker compose exec worker python -m app.pipeline delete $(ID)

unstick:
	docker compose exec worker python -m app.pipeline unstick

reset:
	docker compose exec worker python -m app.pipeline reset-db
