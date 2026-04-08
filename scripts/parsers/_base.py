"""Parser contract — every parser module must conform to this interface."""

from typing import Protocol


class ParserModule(Protocol):
    EXTENSIONS: list[str]
    LANGUAGE: str

    @staticmethod
    def parse(source: str, file_path: str) -> list[dict]: ...
