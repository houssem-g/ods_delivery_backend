"""Bilingual (Arabic + French) e-mails. The recipient's language is often unknown at sign-up,
so every message carries both, the user's language first."""

from dataclasses import dataclass
from html import escape
from typing import Literal

from app.services.order_texts import minutes_ar

CodePurpose = Literal["verify", "reset", "migrate"]

_COPY: dict[str, dict[str, tuple[str, str]]] = {
    # purpose -> lang -> (subject, intro)
    "verify": {
        "ar": ("رمز تأكيد بريدك الإلكتروني", "أدخل هذا الرمز في التطبيق لتأكيد بريدك الإلكتروني:"),
        "fr": (
            "Votre code de vérification",
            "Saisissez ce code dans l'application pour confirmer votre e-mail :",
        ),
    },
    "reset": {
        "ar": ("رمز إعادة تعيين كلمة المرور", "أدخل هذا الرمز في التطبيق لاختيار كلمة مرور جديدة:"),
        "fr": (
            "Réinitialisation de votre mot de passe",
            "Saisissez ce code dans l'application pour choisir un nouveau mot de passe :",
        ),
    },
    "migrate": {
        "ar": (
            "مرحباً بك في التطبيق الجديد",
            "انتقل حسابك إلى النسخة الجديدة من التطبيق. أدخل هذا الرمز ثم اختر كلمة مرور جديدة:",
        ),
        "fr": (
            "Bienvenue sur la nouvelle application",
            "Votre compte a été transféré. Saisissez ce code puis choisissez un nouveau mot de passe :",
        ),
    },
}
_VALIDITY = {
    "ar": "صالح لمدة {minutes_ar}. إذا لم تطلب هذا الرمز، تجاهل هذه الرسالة.",
    "fr": "Valable {minutes} minutes. Si vous n'avez rien demandé, ignorez ce message.",
}


@dataclass(frozen=True)
class RenderedEmail:
    subject: str
    text: str
    html: str


_LINK_TEXT = {"ar": "أو افتح هذا الرابط:", "fr": "Ou ouvrez ce lien :"}


def code_email(
    purpose: CodePurpose, code: str, language: str, ttl_minutes: int, link: str | None = None
) -> RenderedEmail:
    order = ["ar", "fr"] if language != "fr" else ["fr", "ar"]
    subject = " / ".join(_COPY[purpose][lang][0] for lang in order)
    text_parts, html_parts = [], []
    for lang in order:
        intro = _COPY[purpose][lang][1]
        validity = _VALIDITY[lang].format(minutes=ttl_minutes, minutes_ar=minutes_ar(ttl_minutes))
        direction = "rtl" if lang == "ar" else "ltr"
        link_text = f"\n{_LINK_TEXT[lang]} {link}" if link else ""
        link_html = (
            f'<p>{escape(_LINK_TEXT[lang])} <a href="{escape(link)}">{escape(link)}</a></p>' if link else ""
        )
        text_parts.append(f"{intro}\n\n    {code}\n{link_text}\n{validity}")
        html_parts.append(
            f'<div dir="{direction}" lang="{lang}" style="font-family:sans-serif;margin-bottom:24px">'
            f"<p>{escape(intro)}</p>"
            f'<p style="font-size:28px;font-weight:bold;letter-spacing:6px">{escape(code)}</p>'
            f"{link_html}"
            f'<p style="color:#666">{escape(validity)}</p></div>'
        )
    return RenderedEmail(
        subject=f"ODS Delivery · {subject}",
        text="\n\n----\n\n".join(text_parts),
        html="<html><body>" + "<hr>".join(html_parts) + "</body></html>",
    )
