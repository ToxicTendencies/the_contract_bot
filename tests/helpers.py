from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakeUser:
    id: int
    first_name: str = "Тест"
    last_name: str = ""
    username: str = "tester"
    is_bot: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


@dataclass
class FakeChat:
    id: int = -100500
    type: str = "supergroup"


class FakeMessage:
    """Only the attributes handlers rely on."""

    def __init__(
        self,
        *,
        text: str = "",
        user: FakeUser | None = None,
        chat: FakeChat | None = None,
        guest_query_id: str | None = "guest-query",
        message_thread_id: int | None = None,
        reply_to_message: object | None = None,
    ):
        self.text = text
        self.reply_to_message = reply_to_message
        self.from_user = user or FakeUser(1)
        self.chat = chat or FakeChat()
        self.guest_query_id = guest_query_id
        self.guest_bot_caller_user = self.from_user
        self.guest_bot_caller_chat = self.chat
        self.message_thread_id = message_thread_id


@dataclass
class FakeCallback:
    data: str
    user: FakeUser
    inline_message_id: str = "inline-1"
    answers: list[str] = field(default_factory=list)
    urls: list[str] = field(default_factory=list)

    @property
    def from_user(self) -> FakeUser:
        return self.user

    async def answer(self, text: str = "", url: str | None = None, **_: object) -> None:
        self.answers.append(text)
        if url:
            self.urls.append(url)


def make_callback(data: str, user_id: int = 1, inline_message_id: str = "inline-1") -> FakeCallback:
    return FakeCallback(data=data, user=FakeUser(user_id), inline_message_id=inline_message_id)


class Recorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def add(self, name: str, **kwargs: object) -> None:
        self.calls.append((name, kwargs))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]
