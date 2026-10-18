from __future__ import annotations

from aiogram.types import Update


def test_dispatcher_subscribes_to_all_entry_points(app):
    used = app.dp.resolve_used_update_types()
    assert "guest_message" in used
    assert "callback_query" in used
    assert "message" in used
    # Inline mode больше не используется: всё происходит в чате.
    assert "inline_query" not in used
    assert "chosen_inline_result" not in used


def test_guest_update_parsing():
    update = Update.model_validate(
        {
            "update_id": 1,
            "guest_message": {
                "message_id": 10,
                "date": 1770000000,
                "chat": {"id": -100123, "type": "supergroup", "title": "Test"},
                "from": {"id": 5, "is_bot": False, "first_name": "Ann"},
                "text": "@test_bot привет",
                "guest_query_id": "abc",
                "reply_to_message": {
                    "message_id": 9,
                    "date": 1770000000,
                    "chat": {"id": -100123, "type": "supergroup", "title": "Test"},
                    "from": {
                        "id": 999,
                        "is_bot": True,
                        "first_name": "Bot",
                        "username": "test_bot",
                    },
                    "text": "contract",
                },
            },
        }
    )
    message = update.guest_message
    assert update.event_type == "guest_message"
    assert message.guest_query_id == "abc"
    assert message.chat.id == -100123
    assert message.from_user.id == 5
    assert message.reply_to_message.from_user.id == 999


def test_preview_markup_fits_callback_limit(app):
    from app import ui

    markup = ui.preview_markup(app.config, "abcdefgh", 1, 3)
    for row in markup.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


def test_contract_markup_fits_callback_limit(app):
    from app import ui

    template = app.config.templates.get("classic")
    markup = ui.contract_markup(app.config, "abcdefgh", template)
    for row in markup.inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


def test_answer_guest_query_payload(app):
    from aiogram.methods import AnswerGuestQuery
    from aiogram.types import InlineQueryResultCachedPhoto

    method = AnswerGuestQuery(
        guest_query_id="abc",
        result=InlineQueryResultCachedPhoto(
            id="1",
            photo_file_id="file-id",
            caption="caption",
            reply_markup=None,
        ),
    )
    payload = method.model_dump(exclude_none=True)
    assert payload["result"]["type"] == "photo"
    assert payload["result"]["photo_file_id"] == "file-id"


def test_tokens_are_url_safe(app):
    from app.store import new_token

    token = new_token()
    assert len(token) == 8
    assert all(char in "0123456789abcdef" for char in token)


def test_every_template_fits_callback_limit(app):
    from app import ui
    from app.store import new_token

    token = new_token()
    for template in app.templates():
        for markup in (
            ui.contract_markup(app.config, token, template),
            ui.preview_markup(app.config, token, 0, len(app.templates())),
        ):
            for row in markup.inline_keyboard:
                for button in row:
                    assert len(button.callback_data.encode()) <= 64


def test_no_date_button_when_template_has_no_dates(app):
    from app import ui

    assert ui.date_button(app.config, "abcdefgh", "") is None
    markup = ui.preview_markup(app.config, "abcdefgh", 0, 5, date="")
    keys = [button.callback_data for row in markup.inline_keyboard for button in row]
    assert all(not key.startswith("dt:") for key in keys)

    with_date = ui.preview_markup(
        app.config, "abcdefgh", 0, 5, date="18.10.2026"
    )
    keys = [button.callback_data for row in with_date.inline_keyboard for button in row]
    assert keys[-1] == "dt:abcdefgh"
