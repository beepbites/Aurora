# MOMOKA/music/plugins/voice_dave_patch.py
"""
Discord DAVE (Discord Audio Visual Encryption) プロトコル対応パッチ。

discord.py 2.x はボイスゲートウェイの DAVE (E2EE) プロトコルに未対応のため、
Discord 側から WebSocket close code 4017 で切断される問題を解消する。

このモジュールは discord.py のボイスWebSocket処理をモンキーパッチし、
以下の変更を適用する:
  1. IDENTIFY ペイロードに max_dave_protocol_version: 0 を追加
     → ボットが DAVE 非対応であることを宣言し、サーバー側に DAVE を強制させない
  2. DAVE 関連オペコード (21-28) をハンドリング
     → 未知のオペコードで例外が発生するのを防止
  3. DAVE_PREPARE_TRANSITION (op 23) への応答
     → DAVE_EXECUTE_TRANSITION (op 24) を送信して遷移を完了させる

参考:
  - Discord Voice Gateway v8 仕様
  - DAVE Protocol (Discord Audio Visual Encryption)
  - Close Code 4017: DAVE プロトコル未対応による切断
"""

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)

# --- DAVE プロトコル関連のオペコード定義 ---
# Discord Voice Gateway v8 で追加された DAVE (E2EE) オペコード
DAVE_MLS_EXTERNAL_SENDER = 21       # MLS外部送信者情報
DAVE_MLS_KEY_PACKAGE = 22           # MLSキーパッケージ
DAVE_PREPARE_TRANSITION = 23        # DAVE暗号化遷移の準備要求
DAVE_EXECUTE_TRANSITION = 24        # DAVE暗号化遷移の実行応答
DAVE_PREPARE_EPOCH = 25             # DAVEエポック準備
DAVE_MLS_INVALID_KEY_PACKAGE = 26   # MLS無効キーパッケージ通知
DAVE_MLS_PROPOSALS = 27             # MLS提案
DAVE_MLS_COMMIT_WELCOME = 28        # MLSコミット＆ウェルカム

# パッチが適用済みかどうかのフラグ
_patched = False


def apply_dave_patch() -> bool:
    """
    discord.py のボイスWebSocketに DAVE プロトコル対応パッチを適用する。

    Returns:
        bool: パッチ適用に成功した場合 True、既に適用済みまたは失敗時は False
    """
    global _patched

    # 二重適用を防止
    if _patched:
        logger.debug("DAVE patch: 既にパッチ適用済みのためスキップ")
        return False

    try:
        from discord.gateway import DiscordVoiceWebSocket
    except ImportError as e:
        logger.error(f"DAVE patch: discord.gateway のインポートに失敗: {e}")
        return False

    # --- 1. IDENTIFY ペイロードに max_dave_protocol_version を追加 ---
    # 元の identify メソッドを保持
    _original_identify = DiscordVoiceWebSocket.identify

    async def _patched_identify(self) -> None:
        """
        DAVE プロトコルバージョン情報を含む IDENTIFY ペイロードを送信する。
        max_dave_protocol_version: 0 → ボットは DAVE E2EE 非対応であることを宣言
        """
        # ボイス接続状態からセッション情報を取得
        state = self._connection
        # DAVE バージョン情報を含んだ IDENTIFY ペイロードを構築
        payload = {
            'op': self.IDENTIFY,
            'd': {
                'server_id': str(state.server_id),
                'user_id': str(state.user.id),
                'session_id': state.session_id,
                'token': state.token,
                # DAVE プロトコルバージョン 0 = E2EE 非対応を宣言
                'max_dave_protocol_version': 0,
            },
        }
        await self.send_as_json(payload)
        logger.debug("DAVE patch: IDENTIFY に max_dave_protocol_version=0 を追加して送信")

    # --- 2. received_message に DAVE オペコードハンドリングを追加 ---
    # 元の received_message メソッドを保持
    _original_received_message = DiscordVoiceWebSocket.received_message

    async def _patched_received_message(self, msg: Dict[str, Any]) -> None:
        """
        受信メッセージを処理する。DAVE 関連オペコードを適切にハンドリングする。
        """
        op = msg.get('op')
        data = msg.get('d', {})

        # DAVE_PREPARE_TRANSITION (op 23) への応答
        # サーバーが DAVE 暗号化遷移を要求した場合、空の EXECUTE_TRANSITION で応答
        if op == DAVE_PREPARE_TRANSITION:
            logger.info(
                f"DAVE patch: DAVE_PREPARE_TRANSITION (op {op}) を受信 → "
                f"DAVE_EXECUTE_TRANSITION (op {DAVE_EXECUTE_TRANSITION}) で応答"
            )
            # 遷移IDを取得（存在する場合）
            transition_id = data.get('transition_id')
            # EXECUTE_TRANSITION 応答ペイロードを構築
            response_payload = {
                'op': DAVE_EXECUTE_TRANSITION,
                'd': {
                    'transition_id': transition_id,
                },
            }
            await self.send_as_json(response_payload)
            # 元の received_message も呼び出してフック処理を実行
            await _original_received_message(self, msg)
            return

        # DAVE_PREPARE_EPOCH (op 25) への応答
        if op == DAVE_PREPARE_EPOCH:
            logger.info(f"DAVE patch: DAVE_PREPARE_EPOCH (op {op}) を受信 → ログのみ")
            # 元の received_message も呼び出してフック処理を実行
            await _original_received_message(self, msg)
            return

        # その他の DAVE オペコード (21, 22, 26, 27, 28) はログのみで無視
        if op in (
            DAVE_MLS_EXTERNAL_SENDER,
            DAVE_MLS_KEY_PACKAGE,
            DAVE_EXECUTE_TRANSITION,
            DAVE_MLS_INVALID_KEY_PACKAGE,
            DAVE_MLS_PROPOSALS,
            DAVE_MLS_COMMIT_WELCOME,
        ):
            logger.debug(f"DAVE patch: DAVE オペコード {op} を受信 → 無視")
            # seq_ack の更新だけは行う
            self.seq_ack = msg.get('seq', self.seq_ack)
            # フック処理を実行
            if hasattr(self, '_hook'):
                await self._hook(self, msg)
            return

        # DAVE 以外のオペコードは元の処理に委譲
        await _original_received_message(self, msg)

    # --- 3. RESUME ペイロードにも DAVE 情報を追加 ---
    # 元の resume メソッドを保持
    _original_resume = DiscordVoiceWebSocket.resume

    async def _patched_resume(self) -> None:
        """
        DAVE プロトコルバージョン情報を含む RESUME ペイロードを送信する。
        """
        state = self._connection
        payload = {
            'op': self.RESUME,
            'd': {
                'token': state.token,
                'server_id': str(state.server_id),
                'session_id': state.session_id,
                'seq_ack': self.seq_ack,
                # DAVE プロトコルバージョン 0 = E2EE 非対応を宣言
                'max_dave_protocol_version': 0,
            },
        }
        await self.send_as_json(payload)
        logger.debug("DAVE patch: RESUME に max_dave_protocol_version=0 を追加して送信")

    # --- パッチの適用 ---
    DiscordVoiceWebSocket.identify = _patched_identify
    DiscordVoiceWebSocket.received_message = _patched_received_message
    DiscordVoiceWebSocket.resume = _patched_resume

    # パッチ適用済みフラグを設定
    _patched = True
    logger.info(
        "DAVE patch: discord.py ボイスWebSocketに DAVE プロトコル対応パッチを適用しました "
        "(max_dave_protocol_version=0, DAVE opcodes 21-28 handled)"
    )
    return True


def is_patched() -> bool:
    """
    DAVE パッチが適用済みかどうかを返す。

    Returns:
        bool: パッチが適用済みの場合 True
    """
    return _patched
