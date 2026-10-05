# notion-webclip-summarizer

Notion Web Clipperの未要約記事を日本語で要約し、関連タグ5つと要約日時を更新する単発バッチです。`lifelog_recorder` のユースケースとタイトル整形ルールを参考に、本文取得・LLM・出力・バッチを分離して再構築しました。

## セットアップ

Python 3.11以上と[uv](https://docs.astral.sh/uv/)を使用します。

```sh
cp .env.example .env
uv sync --frozen
uv run webclip-summarizer --limit 10
```

`.env` に `NOTION_TOKEN`, `NOTION_DATABASE_ID`, `LLM_MODEL` と選択したプロバイダーのAPIキーを設定してください。Notionのインテグレーションに対象DBを共有し、読み取り・コンテンツ更新権限を与えます。新しいタグの追加には対象データソースを編集できる権限が必要です。

DBには次のプロパティを作成してください。既存のプロパティを自動変更することはありません。

| プロパティ | Notion型 | 用途 |
| --- | --- | --- |
| 任意の名前 | title | 記事タイトル（自動検出） |
| summary | rich_text | 要約 |
| tag | multi_select | 関連タグ5つ |
| summary_time | date | 要約完了日時 |
| 任意の名前（任意） | url | 出典URL（最初のURL型プロパティを採用） |

出力・更新先プロパティ名は `.env.example` の設定で変更できます。記録日時はページの `created_time` を採用します。[Notion API 2025-09-03](https://developers.notion.com/docs/upgrade-guide-2025-09-03)のdata source構成に対応し、DBのソースが1つなら自動解決、複数なら `NOTION_DATA_SOURCE_ID` が必須です。

## LLM切り替え

[LiteLLM](https://docs.litellm.ai/docs/providers)でプロバイダー差分を吸収します。モデル名は各プロバイダーで利用可能なものを指定してください。例のモデルは契約・提供状況に応じて変更できます。

| 提供元 | LLM_MODELの形式 | APIキー環境変数 |
| --- | --- | --- |
| OpenAI | `openai/<model>` | OPENAI_API_KEY |
| Claude | `anthropic/<model>` | ANTHROPIC_API_KEY |
| Gemini | `gemini/<model>` | GEMINI_API_KEY |
| Grok | `xai/<model>` | XAI_API_KEY |
| OpenRouter | `openrouter/<provider>/<model>` | OPENROUTER_API_KEY |

本文・候補タグはLLMへ送信します。直近100件のタグ付き記事を参照し、頻度順の候補（最大300タグ）から意味の近いタグを優先選択するよう指示します。要約は300〜400文字を目安に「主題・結論／根拠・具体例／示唆・注意点」の3項目で生成します。短い記事では事実を水増しせず、目安より短い要約を許容します。タグは日本語・英語や略語の違いも含めて同義・類似の既存タグを優先し、新規タグは日本語を基本にします。既存の英語タグや固有名詞の表記は維持します。文字数や意味的な同義性はプロンプトによる指示で、厳密な保証ではありません。同義性の判断はLLMが行い、表記差はプログラムで既存タグへ統一します。1回の実行で作成したタグも次の記事の候補に追加します。`RECENT_ARTICLE_COUNT` で参照件数を変更できます。

本文はNotionのブロックをページ送り・子ブロック込みで取得します。見出し、リスト、コード、表、画像、リンクをMarkdownへ変換します。画像OCRやリンク先Webページの取得は行いません。本文が空なら要約せず、未処理状態を維持します。長文は `LLM_CHUNK_CHARS` 単位で階層的に要約し、末尾を単純に切り捨てません。巨大な記事ほどLLM呼び出し回数と費用が増加します。

要約結果のJSON・5つの重複しないタグを検証し、不正な応答は再生成します。成功時だけ `summary`, `tag`, `summary_time` を1回のPATCHで更新します。失敗した記事は未要約のまま残し、他の記事は処理を継続します。Notionの429・5xx・通信失敗は再試行します。

## 開発・検証

```sh
uv run pytest
uv run ruff check src tests
uv run ruff format --check src tests
```

テストは実サービスやAPIキーを使わず、ページ送り・子ブロック取得、長文の末尾、タグ検証、更新の失敗分離を確認します。

## OpenRouterの429（レート制限）

`temporarily rate-limited upstream` は、モデル提供元の共有枠が一時的に混雑している状態です。LLM呼び出しは `tenacity` を使い、待ち時間を増やしながら有限回の再試行を行います。制限が続く場合は記事を未処理のまま残し、次回実行で再処理します。依存追加後は `uv sync --frozen` を実行し、Dockerを利用している場合はイメージを再ビルドしてください。

時間を置いて再実行しても429が続く場合は、`LLM_MODEL` を別の利用可能なモデルへ変更するか、OpenRouterのプロバイダーキー連携を設定してください。課金を伴うモデルへの自動切り替えは行いません。

## OpenRouterの`:batch`モデル

このアプリは同期型の `chat/completions` を利用するため、`openrouter/openai/gpt-5-nano:batch` のようなBatch専用モデルは使えません。設定時点でエラーを返し、Notion・LLMへのアクセス前に終了します。同期処理には `LLM_MODEL=openrouter/openai/gpt-5-nano` のように通常モデルを指定してください。通常モデルはBatchモデルと料金が異なります。Batch APIのジョブ投入・結果待ち・結果回収は未対応です。

## 推論モデルの空応答・出力上限

推論トークンも出力上限に含まれるため、GPT-5 nano等では上限に達して回答本文が空になることがあります。`LLM_MAX_TOKENS` は標準8192で、`finish_reason=length` の場合は1回だけ上限を2倍にして再試行します。空応答も1回だけ再試行し、拒否・コンテンツフィルターの場合は再試行しません。途中で切れた本文は要約に採用しません。ログにはモデル、終了理由、回答・推論トークン数、上限を記録し、記事本文や推論内容は出力しません。

`LLM_REASONING_EFFORT` が空なら通常はモデル標準の設定を使います。ただし `openrouter/openai/gpt-5-nano` と `openai/gpt-5-nano` は `minimal` を自動指定します。必要に応じて `low` 等を明示できます。指定値の対応状況は利用モデルによって異なります。上限増加や再試行は利用費用を増やす可能性があります。
