# 원격 git 작업 (remote-ops)

SSH·원격으로 닿을 수 없는 강의실 PC의 레포가 틀어졌을 때(예: `AutoUpdate`가 병합 충돌로 멈춤),
**레포 소유자**가 GitHub를 통해 고치는 길입니다. 닿는 수단이 GitHub 이슈·PR·레포뿐이라는 전제에서
만들었습니다.

## 아무 명령이나 실행하는 원격 셸이 아닙니다

정규화한 **git 작업만** 실행합니다(`src/syncnote/remote_ops.py`의 허용 목록, Pro Git 문서에서 고름).
임의 셸·`clean -x/-X`·`prune --expire`·reflog 삭제·`filter-branch`는 목록에 없습니다.

한 줄이라도 실행하려면 네 가지를 **모두** 통과해야 합니다.

1. **서명** (가장 강한 문) — 계획이 미리 나눠 가진 비밀로 HMAC-SHA256 서명돼 있어야 합니다. 비밀이
   없거나 서명이 틀리면 아무것도 실행하지 않습니다. 레포에 글을 쓸 수 있어도 소용없습니다.
2. **소유자** — 계획을 올린 계정이 이 레포 admin(설정 `owner_login`을 적으면 그 계정) 이어야 합니다.
   Gist는 `gist.owner.login`, 이슈는 작성자 로그인으로 확인합니다.
3. **재생·만료 방지** — 계획의 `seq`(점점 커지는 수)·`id`·`expires_at`. 지난 것·쓴 것·만료된 것은 거부.
4. **허용 목록 + 검증** — `op`는 허용 목록에 있는 것만, 인자는 안전한 글자만(옵션 주입 차단, 경로는
   레포 안). 위험한 작업은 계획의 `confirm_destructive` + 설정 `allow_destructive` + **녹화 중 아님**일 때만.

## 처음 한 번 (소유자가 PC에 접근할 수 있을 때)

각 강의실 PC에서 서명 비밀을 넣어 둡니다. 이게 없으면 원격 작업은 영영 inert입니다.

```
python -m syncnote secrets set remote_ops
```

소유자 PC에도 같은 비밀을 둡니다(서명에 씁니다). 그리고 레포에 `remote-ops` 라벨을 만들어 둡니다.

## 보내는 법 (소유자)

```
python -m syncnote remote-ops new --target HUM-4F-1436 --reason "AutoUpdate 충돌 복구" > plan.json
# plan.json 의 steps 를 고친 뒤
python -m syncnote remote-ops sign plan.json      # 서명된 JSON 출력
```

서명된 JSON을 **Gist로 올리고**(소유자 계정), 그 Gist 링크를 `remote-ops` 라벨 이슈에 올립니다.
(또는 서명된 JSON을 이슈 본문의 ```json 블록에 바로 붙여도 됩니다.)

PC가 받아 실행하고, 결과(각 단계의 종료 코드·출력)를 그 이슈에 댓글로 남긴 뒤 닫습니다.

## 계획 형식

```json
{
  "version": 1, "id": "<uuid>", "seq": 1795685789,
  "created_at": "2026-11-26T15:00:00Z", "expires_at": "2026-11-27T15:00:00Z",
  "target": "HUM-4F-1436",            // 또는 "all" 또는 ["HUM-4F-1436","HUM-2F-1244"]
  "reason": "AutoUpdate 충돌 복구",
  "stop_on_error": true, "confirm_destructive": true,
  "steps": [
    {"op": "status"},
    {"op": "stash-push", "msg": "remote-ops backup"},   // 로컬 수정을 먼저 안전하게 백업
    {"op": "fetch", "remote": "origin", "branch": "main", "prune": true},
    {"op": "reset-hard", "ref": "origin/main"},
    {"op": "clean", "dry_run": true}
  ],
  "sig": "<hmac-sha256>"
}
```

## 허용 작업

| 분류 | 작업 |
|---|---|
| 읽기(언제나) | `status` `log` `reflog` `fsck` `remote` `rev-parse` `count-objects` `branch-list` `show` `stash-list` |
| 동기화(어긋나면 안 바꾸고 실패) | `fetch` `pull-ff` `merge-ff` `switch` `checkout-branch` |
| 안전(되돌리거나 백업) | `merge-abort` `revert` `revert-abort` `stash-push` `stash-apply` `stash-pop` `branch-create` |
| 위험(confirm+설정+녹화 아님) | `reset-hard` `clean`(`-f -d`, `-x`/`-X` 없음) `checkout-file` `branch-delete-force` |

`reset --hard`로 잃은 커밋은 `reflog`·`fsck`로 찾아 `branch-create`로 되살릴 수 있습니다(위 표의 안전 작업).

## 설정 `[remote_ops]`

`enabled`(기본 true, 그래도 서명 비밀이 없으면 inert) · `repo`(비우면 `[github]`) · `owner_login`(한
사람으로 못박기, 강력 권장) · `require_admin` · `allow_destructive` · `poll_interval_minutes`.

직접 확인: `python -m syncnote remote-ops status`, 한 번 받기: `python -m syncnote remote-ops poll [--dry-run]`.
