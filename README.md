# knowledge-search

LAN 内の 1 台のサーバーで動かす個人知識ストア。OpenSearch 3.9.0 に kuromoji と ngram を載せ、固有知識・活動履歴・チャットログ・テレメトリを同じ索引にためる。

エージェントはベクトル類似ではなく、クエリを自分で書いてキーワード検索する。形態素（走りました → 走る）は kuromoji、辞書に無い固有句は 2-3gram が拾う。

## なぜ 3.9.0 か

2026-09-29 公開の最新版。公式ダウンロードページは 3.8.0 のまま遅れることがある。実体は Docker Hub の `opensearchproject/opensearch:3.9.0`。

メモリ 4GB 未満のホストを想定している（公式の Docker 手順は 4GB を要求する）。ヒープは 512MB、コンテナ上限は 1400MB。k-NN / ML プラグインは外してある。収まらなければ別 VPS に移す。

ホストの `vm.max_map_count` は 262144 にする（`/etc/sysctl.d/99-opensearch.conf` などで永続化）。mmap は有効。2.x のデータディレクトリは 3.9 では開けない。

## 起動

初回はイメージ取得とプラグイン導入で数分かかる。健康確認の待ちは 3 分以上見てから失敗扱いにすること。

```bash
cd ~/knowledge-search   # このリポジトリを置いた場所
cp .env.example .env   # 既にある .env は上書きしない
chmod 600 .env
docker compose up -d --build --wait --wait-timeout 600
```

以下の例ではサーバーのアドレスをダミーの `192.0.2.10` で書く。自分のサーバーのアドレスに置き換える。

- API: `http://192.0.2.10:9470`（LAN。Bearer 必須）
- MCP SSE: `http://192.0.2.10:9471/sse`（LAN 上の全クライアントから。Bearer `KS_API_KEY` 必須）
- OpenSearch: `http://127.0.0.1:9200`（デバッグ用。エージェントは叩かない）

秘密は `.env` の `KS_API_KEY`。リポジトリには入れない。

## コマンド

```bash
scripts/ks ingest ./notes --kind knowledge --tag project
echo '本文' | scripts/ks put --kind telemetry --title 'cron ok' --source-id job-1 --agent cron
scripts/ks search '納品書の照合'
scripts/ks get <doc_id>
scripts/ks tag <doc_id> --add reviewed
scripts/ks list --kind chat
scripts/ks delete <doc_id>
scripts/ks stats
python3 scripts/ingest_hermes_sessions.py --db ~/.hermes/state.db --host host-a
```

同じ `source_id`（ファイルなら相対パス）で再投入すると上書きする。重複しない。

### Hermes セッションの自動保存（各マシン共通）

各 Hermes マシンで一度だけ実行する。何度実行しても安全。

```bash
git clone https://github.com/<owner>/knowledge-search.git ~/knowledge-search   # 既にあれば git pull
python3 ~/knowledge-search/scripts/install_client.py --label host-a \
  --api-url http://192.0.2.10:9470 --env-file ~/secrets/knowledge-search.env   # KS_API_KEY を書いたファイル
hermes gateway restart
```

`install_client.py` が行うこと:

- `~/.hermes/.env` に `KS_API_URL` / `KS_API_KEY` / `KS_HOST_LABEL` を書く（chmod 600）。鍵は `~/.hermes/.env`、`--env-file`、リポジトリの `.env`（サーバー）、`~/.config/knowledge-search/client.env` の順で探す。鍵はリポジトリに置かず、各マシンへは自分の方法（パスワードマネージャーなど）で配る。
- スキル `hermes/skills/knowledge-search/SKILL.md` を `~/.hermes/skills/devops/knowledge-search/` へコピーする。
- MCP `knowledge-search`（`http://<server>:9471/sse`、ヘッダ `Authorization: Bearer ${KS_API_KEY}`）を登録する。
- そのマシンの過去セッションを一度だけ全部保存し、cron `knowledge_autosave`（no_agent、10 分ごと）を作る。本体は `scripts/hermes_autosave.py`。

差分の仕組み:

- `~/.hermes/state/knowledge_autosave.json` に最後に保存した `messages.id` を記録し、次回はその続きから読む。
- 毎回、直近 300 件を送り直す。`source_id` が `<label>:<message id>` なので上書きになり、重複しない。
- ラベルはマシンごとに一意にする。既に会話があるラベルはインストーラが拒否する（そのマシンのデータなら `--adopt`）。
- 成功時は何も出力しない。失敗時だけ短い通知を出し、cron が届ける。ログは `~/.hermes/logs/knowledge_autosave.log`。
- Hermes の `hooks.outbound`（`post_llm_call`）は使わない。メッセージ id を持たないため重複し、失敗した送信は再送されず失われる。

### エージェントのメモリを知識として保存

自動保存（Hermes の cron と Claude.app の LaunchAgent）は、会話に続けてメモリも `kind=knowledge` で保存する。本体は `scripts/ingest_memories.py`。

- Claude Code: `~/.claude/projects/<project>/memory/*.md`。1 ファイル 1 件。索引の `MEMORY.md` は除く。タグは `memory` / ラベル / `claude` / プロジェクト名 / メモリの種類。
- Hermes: `~/.hermes/memories/MEMORY.md` と `USER.md`。`§` で区切った 1 項目を 1 件にする。タグは `memory` / ラベル / `hermes` / `memory` か `user`。
- 変わったものだけ送り、ディスクから消えたものは知識ストアからも消す。送った内容は `~/.config/knowledge-search/claude_memories.json`・`~/.hermes/state/knowledge_memories.json` に記録する。
- 各 Hermes マシンは `git pull` するだけで次の cron から対象になる。

### Hermes スキルの知識化（各マシン共通・自動）

同じ cron（`knowledge_autosave`）が会話の保存に続けて `scripts/ingest_hermes_skills.py` を実行し、そのマシンのスキルを `kind=knowledge`（タグ `skill`）として保存する。1 スキル 1 文書で、`SKILL.md` と同じディレクトリ内の `*.md`（`references/` など）をまとめる。

対象の判定（`classify()`、上から順に）:

1. 同梱スキル（`.bundled_manifest`）は、元のハッシュから変わっている＝ユーザーが手を入れたものだけ入れる。
2. Hermes が作成・編集を記録したスキル（`.usage.json` の `created_by: agent`、`.curator_ledger.jsonl` の create/patch など）は入れる。
3. Hub から入れたスキル（`.hub/lock.json`）は除外する。
4. Hermes 本体が現在または過去に同梱した名前（ソースツリーと git 履歴で判定）は、古い同梱スキルの残骸として除外する。
5. それ以外は、記録の仕組みができる前に作られた自作スキルとして入れる。

`python3 scripts/ingest_hermes_skills.py --dry-run` で対象と除外理由を一覧できる。

複数マシンの扱い:

- `source_id` は `skill:<skills/ からの相対パス>` でマシン名を含まない。同じスキルは全マシンで 1 文書になる。
- 版が食い違うときは、ファイルの更新時刻が新しいほうを残す。古い版を持つマシンは上書きしない（「新しい `created_at` を正とする」規則と同じ）。
- 変更のないスキルは送らない（`~/.hermes/state/knowledge_skills.json` に内容のハッシュを記録）。
- スキルがマシンから消えたら、そのマシンが最後に書いた文書だけをストアから消す。
- コードの更新は各マシンで `git -C ~/knowledge-search pull` すれば次の cron から反映される。再インストールは不要。

### Claude.app（Mac の Claude Desktop・Cowork・Code タブ）

Mac ごとに一度だけ実行する。何度実行しても安全。鍵と URL は `~/.hermes/.env` から読む。無ければ `--env-file` と `--api-url` で渡す。

```bash
python3 ~/knowledge-search/scripts/install_claude_app.py --label host-a-claude
# 終わったら Claude.app を Cmd+Q で終了して開き直す
```

`install_claude_app.py` が行うこと:

- `~/.config/knowledge-search/client.env` に `KS_API_URL` / `KS_MCP_URL` / `KS_API_KEY` / `KS_HOST_LABEL` を書く（chmod 600）。
- MCP `knowledge-search` を `claude_desktop_config.json`（チャット・Cowork・Code タブ）と Claude Code の user スコープ（ターミナルの `claude`）に登録する。中身は stdio→SSE のブリッジ `scripts/mcp_stdio_bridge.py`。鍵は設定ファイルに書かず、ブリッジが `client.env` から読む。Claude.app のリモートコネクタは Anthropic のクラウドから接続するため LAN のサーバーに届かない。だから stdio で中継する。
- 起動中の Claude.app は `claude_desktop_config.json` を自分の持つ内容で書き戻すため、起動中に足した項目は消える。インストーラは起動中ならワンショットの LaunchAgent `local.knowledge-search.claude-register` を置き、Cmd+Q で終了した後に書き込んで Claude.app を開き直す。ログは `~/.config/knowledge-search/logs/claude_register.log`。
- Code タブ／Claude Code（`~/.claude/projects/*/*.jsonl`）と Cowork（`~/Library/Application Support/Claude/local-agent-mode-sessions/.../.claude/projects/*/*.jsonl`）の過去セッションを一度だけ全部保存する。本体は `scripts/ingest_claude_sessions.py`。
- LaunchAgent `local.knowledge-search.claude-autosave`（10 分ごと）を作る。本体は `scripts/claude_autosave.py`。ログは `~/.config/knowledge-search/logs/claude_autosave.log`、失敗時は macOS の通知を出す。

保存の仕方:

- `agent` は `claude`、タグは `claude` / ラベル / `code` か `cowork`。`source_id` は `<label>:<行の uuid>`。
- 転記ファイルごとに保存済みのバイト位置を `~/.config/knowledge-search/claude_autosave.json` に記録し、追記分だけ送る。タイトルが変わったファイルは全体を送り直す（上書きなので重複しない）。
- ユーザーとアシスタントの本文を保存する。思考・ツール結果・サブエージェントは保存しない。ツール呼び出しは名前だけ残す。
- 通常のチャット（claude.ai の会話）は Anthropic のサーバーにあり、Mac に転記が無いので保存しない。

## API

すべて `Authorization: Bearer $KS_API_KEY`。

| メソッド | パス | 用途 |
|---|---|---|
| POST | `/v1/documents` | 1件書き込み。エージェントとテレメトリ |
| POST | `/v1/documents/bulk` | JSON で最大 200 件 |
| POST | `/v1/files` | テキストファイルの multipart 一括。`kind` と `tags` は form |
| POST | `/v1/search` | 検索。`kind` `tags` `agent` `session_id` `since` `until` |
| GET | `/v1/documents/{doc_id}` | 全文（チャンクを結合） |
| GET | `/v1/documents` | 新しい順の一覧 |
| PATCH | `/v1/documents/{doc_id}/tags` | タグの追加・削除。整理用 |
| DELETE | `/v1/documents/{doc_id}` | 削除 |
| GET | `/v1/stats` | 件数 |
| GET | `/health` | 認証なし。中身は出さない |

`kind` は `knowledge` / `activity` / `chat` / `telemetry` / `file`。未知の値も受け付ける。

検索のあと全文が要るときは `doc_id` で GET する。ヒットはチャンク単位で、スニペットは `【】` で囲まれる。

## Hermes MCP

`~/.hermes/config.yaml` に次を足し、Hermes を再起動する。再起動しないとツールは出ない。

```yaml
mcp_servers:
  knowledge-search:
    url: "http://192.0.2.10:9471/sse"
    transport: sse
    timeout: 120
    connect_timeout: 60
```

ツール: `knowledge_search` / `knowledge_get` / `knowledge_put` / `knowledge_list` / `knowledge_tag`。

他マシンのエージェントは MCP ではなく API を使う。

```bash
curl -sS -X POST http://192.0.2.10:9470/v1/documents \
  -H "Authorization: Bearer $KS_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"kind":"telemetry","title":"ping","body":"exit 0","source_id":"job-1","agent":"cron"}'
```

## 落とし穴

- OpenSearch の 9200 を LAN に開けない。認証は API 側だけにある。
- セキュリティプラグインは無効。鍵の管理は `.env` の chmod 600。
- 起動直後に短いタイムアウトで切らない。このホストでは OpenSearch の健康確認に 3 分以上かかることがある。
- 索引のアナライザは作成後に変えられない。変えるときは索引を作り直して再投入する。
- 1 ファイル 25MB まで。長い本文は約 1600 字で分割して検索し、GET で結合して返す。
