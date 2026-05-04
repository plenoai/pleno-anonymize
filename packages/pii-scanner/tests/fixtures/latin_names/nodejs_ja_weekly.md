# 週刊 Node.js (excerpt)

このファイルは Issue #102 のリグレッションテスト用フィクスチャです。
nodejs/nodejs-ja の weekly/2015-05-22.md を抜粋した形で再現しています。

## io.js v2.0.2

- `--require` の前に他のフラグが使用された際に事前にロードしたモジュールが動かない問題が修正されました。(Yosuke Furukawa) [#1635](https://github.com/nodejs/io.js/pull/1635)
- `send()` のコールバックが非同期でなかったのが修正されました。(Yosuke Furukawa) [#1313](https://github.com/nodejs/io.js/pull/1313)
- キャッチされないエラーはその状況を提供するようになりました。(Evan Lucas) [#1654](https://github.com/nodejs/io.js/pull/1654)
- ストリーム処理の改善 (Roman Reiss) [#1696](https://github.com/nodejs/io.js/pull/1696)
