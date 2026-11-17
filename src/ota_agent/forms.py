from html.parser import HTMLParser

from .errors import OTAError


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.fields: dict[str, str] = {}
        self.action: str | None = None

    def handle_starttag(self, tag, attrs):
        attr = dict(attrs)
        if tag == "form" and self.action is None:
            self.action = attr.get("action", "")
        if tag == "input" and attr.get("name"):
            kind = attr.get("type", "text").lower()
            if kind in {"button", "submit", "reset", "file"}:
                return
            if kind in {"radio", "checkbox"} and "checked" not in attr:
                return
            self.fields[attr["name"]] = attr.get("value", "")


def parse_form(page: str) -> FormParser:
    form = FormParser()
    form.feed(page)
    if form.action is None:
        raise OTAError("上游未返回预期表单。")
    return form
