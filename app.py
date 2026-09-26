import base64
import html
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI

import accounts
import drafts
import sources

BASE_DIR = Path(__file__).parent

# Load settings from the .env file next to this script
load_dotenv(BASE_DIR / ".env")

TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-6-luna")
IMAGE_MODEL = os.getenv("OPENAI_IMAGE_MODEL", "gpt-image-2.5-flare")
IMAGE_SIZE = "1536x800"  # close to LinkedIn's 1.91:1 landscape format
LINKEDIN_MAX_CHARS = 3000
PREVIEW_MAX_CHARS = 210  # LinkedIn hides the rest behind "...ver mais"
DEVICE_MAX_CHARS = {"Computador": PREVIEW_MAX_CHARS, "Telemóvel": 140}  # phones cut earlier
PREVIEW_MAX_LINES = 3  # ...or after the third line, whichever comes first

client = OpenAI()  # reads OPENAI_API_KEY from the environment

TONES = {
    "Profissional": "profissional, claro e credível",
    "Descontraído": "descontraído, próximo e com algum humor leve",
    "Inspirador": "inspirador, motivador e com energia positiva",
}

# (Material icon, title, topic) shown in the "Ideias para posts" card
IDEAS = [
    ("rocket_launch", "Lançamento de produto", "a nossa empresa lançou um chatbot para restaurantes"),
    ("celebration", "Marco da empresa", "chegámos aos 100 clientes em Portugal"),
    ("person_add", "Recrutamento", "estamos a contratar um programador Python júnior"),
    ("event", "Evento", "vamos estar na Web Summit em Lisboa, venham falar connosco"),
    ("school", "Aprendizagem", "o que aprendi no meu primeiro mês a trabalhar com IA"),
]

IMAGE_STYLES = {
    "Fotografia": "fotografia realista, luz natural, qualidade editorial",
    "Ilustração": "ilustração digital moderna, flat design, formas simples",
    "3D": "render 3D minimalista, materiais suaves, iluminação de estúdio",
}

LOGIN_PROVIDERS = {"google": "Google", "linkedin": "LinkedIn"}

MEDIA_MODES = ["Sem imagem", "Gerar com IA", "Anexar ficheiro"]
MEDIA_TYPES = ["png", "jpg", "jpeg", "gif", "webp", "mp4", "mov"]

VERIFICATION_HELP = (
    "A OpenAI pede que a organização esteja **verificada** para gerar imagens. "
    "Pede a quem gere a conta (o teu chefe) para ir a "
    "[platform.openai.com/settings/organization/general]"
    "(https://platform.openai.com/settings/organization/general), carregar em "
    "**Verify Organization** e seguir os passos. Pode demorar até 30 minutos a ficar ativo. "
    "Entretanto, podes usar **Anexar ficheiro**."
)

# Image types the image model can edit, with the file extension it expects
EDITABLE_IMAGES = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}

# (button label, instruction) quick edits shown under the image
QUICK_IMAGE_EDITS = [
    ("Mais azul", "Usa uma paleta com mais tons de azul."),
    ("Minimalista", "Torna a imagem mais minimalista e limpa, com menos elementos."),
    ("Mais luminosa", "Deixa a imagem mais clara e luminosa."),
    ("Ilustração", "Transforma a imagem numa ilustração digital moderna, em flat design."),
]

SOURCE_MODES = ["Tema", "Link", "Documento"]

# Writing angles used when the user asks for 3 versions
ANGLES = {
    "História": "Conta-o como uma pequena história da equipa ou da empresa.",
    "Direto": "Vai direto ao ponto, com frases curtas e sem rodeios.",
    "Em lista": "Organiza o corpo do post numa lista curta de pontos fáceis de ler.",
}

# (Material icon, button label, instruction for the model) for the "Ferramentas IA" card
QUICK_TOOLS = [
    ("compress", "Mais curto", "Encurta o post para cerca de metade, mantendo a mensagem principal, o gancho e as hashtags."),
    ("business_center", "Mais formal", "Reescreve o post com um tom mais formal e corporativo."),
    ("sentiment_satisfied", "Mais descontraído", "Reescreve o post com um tom mais descontraído e próximo, com algum humor leve."),
    ("bolt", "Novo gancho", "Reescreve só a primeira linha com um gancho mais forte e surpreendente. Mantém o resto igual."),
    ("forum", "Acabar com pergunta", "Termina o post com uma pergunta aberta que incentive comentários, antes das hashtags."),
    ("translate", "Traduzir para inglês", "Traduz o post para inglês natural e profissional, adaptando as hashtags."),
]


def post_request(topic: str, tone: str, angle: str = "", source: dict | None = None) -> dict:
    """Arguments for the text model: a LinkedIn post about the topic (or a link/document)."""
    if source:
        request = "Escreve um post para o LinkedIn a partir da fonte em anexo."
        if topic.strip():
            request += f"\nO que destacar: {topic.strip()}"
    else:
        request = f"Tema: {topic}"
    content = [{"type": "input_text", "text": f"{request}\nTom: {TONES[tone]}\n{angle}".strip()}]
    if source:
        content.append(source)

    return dict(
        model=TEXT_MODEL,
        instructions=(
            "És um especialista em comunicação no LinkedIn. "
            "Escreve sempre em português de Portugal (pt-PT), nunca em português do Brasil. "
            "Escreve um post pronto a publicar: um gancho forte na primeira linha, "
            "parágrafos curtos, alguns emojis com moderação, uma chamada à ação no fim "
            "e 3 a 5 hashtags relevantes na última linha. "
            "Se receberes um artigo ou documento, resume as ideias principais e comenta-as "
            "numa perspetiva profissional; não inventes factos, números ou citações que não "
            "estejam na fonte. Se a fonte for um link, não o escrevas no texto (o LinkedIn "
            "reduz o alcance de posts com links); em vez disso, escreve «Link nos comentários 👇» logo antes das hashtags. "
            "Devolve apenas o texto do post, sem comentários extra."
        ),
        input=[{"role": "user", "content": content}],
    )


def generate_post(topic: str, tone: str, angle: str = "", source: dict | None = None) -> str:
    return client.responses.create(**post_request(topic, tone, angle, source)).output_text


def stream_post(topic: str, tone: str, angle: str = "", source: dict | None = None):
    """Like generate_post, but yields the text piece by piece as the model writes it."""
    for event in client.responses.create(**post_request(topic, tone, angle, source), stream=True):
        if event.type == "response.output_text.delta":
            yield event.delta


def build_source(mode: str, url: str, document) -> dict | None:
    """The link or document to write about, as model input. Raises ValueError (pt-PT)."""
    if mode == "Link":
        if not sources.is_valid_url(url):
            raise ValueError("Cola um link válido, a começar por https://")
        with st.spinner("A ler o link..."):
            article = sources.fetch_article(url)
        return {"type": "input_text", "text": f"Artigo ({url.strip()}):\n{article}"}
    if mode == "Documento":
        if document is None:
            raise ValueError("Carrega primeiro um documento.")
        return sources.document_input(document.name, document.getvalue())
    return None


def choose_version() -> None:
    """Show the version picked in the 'Versões' selector."""
    choice = st.session_state.get("version_choice")
    if choice is None:  # clicking the selected pill again unselects it; keep the current post
        return
    chosen = st.session_state["versions"][list(ANGLES).index(choice)]
    if chosen != st.session_state.get("post_text"):
        st.session_state["post_history"].append(st.session_state.get("post_text", ""))
        st.session_state["post_text"] = chosen
        st.session_state["last_tool"] = f"Versão {choice}"


def rewrite_post(post: str, instruction: str) -> str:
    """Ask the text model to apply one change to the post."""
    response = client.responses.create(
        model=TEXT_MODEL,
        instructions=(
            "És editor de posts do LinkedIn. Aplica ao post a alteração pedida. "
            "Escreve em português de Portugal (pt-PT), exceto se a alteração pedir outra língua. "
            "Mantém as hashtags na última linha, exceto se a alteração disser o contrário. "
            "Devolve apenas o post final, sem comentários extra."
        ),
        input=f"Alteração: {instruction}\n\nPost:\n{post}",
    )
    return response.output_text


def undo_rewrite() -> None:
    """Go back to the version before the last AI tool."""
    st.session_state["post_text"] = st.session_state["post_history"].pop()
    st.session_state["last_tool"] = None
    # Keep the "Escolhe a versão" selector in sync with the restored text
    versions = st.session_state.get("versions") or []
    if st.session_state["post_text"] in versions:
        st.session_state["version_choice"] = list(ANGLES)[versions.index(st.session_state["post_text"])]
    elif versions:
        st.session_state["version_choice"] = None


def generate_image(subject: str, style: str) -> bytes:
    """Ask the image model for a picture to go with the post; returns PNG bytes."""
    prompt = (
        "Imagem para acompanhar um post profissional no LinkedIn. "
        f"Tema: {subject}. Estilo: {IMAGE_STYLES[style]}. "
        "Composição limpa e moderna, com tons de azul harmoniosos. "
        "Sem texto, sem letras e sem logótipos na imagem."
    )
    result = client.images.generate(model=IMAGE_MODEL, prompt=prompt, size=IMAGE_SIZE)
    return base64.b64decode(result.data[0].b64_json)


def store_upload(key: str) -> None:
    """Keep the uploaded file as the post media (or clear it when removed)."""
    file = st.session_state[key]
    if file is not None:
        st.session_state["media"] = {
            "data": file.getvalue(),
            "mime": file.type,
            "name": file.name,
            "source": "upload",
        }
        st.session_state["media_history"] = []
    elif st.session_state.get("media", {}).get("source") == "upload":
        del st.session_state["media"]


def make_ai_image(subject: str, style: str) -> None:
    """Generate an AI image for the post; on failure, keep a friendly error to show."""
    try:
        st.session_state["media"] = {
            "data": generate_image(subject, style),
            "mime": "image/png",
            "name": "postmind-imagem.png",
            "source": "ai",
        }
        st.session_state["image_request"] = (subject, style)
        st.session_state["media_history"] = []
    except Exception as error:
        if "verif" in str(error).lower():
            st.session_state["image_error"] = VERIFICATION_HELP
        else:
            st.session_state["image_error"] = f"O post foi criado, mas a imagem falhou: {error}"


def remove_media() -> None:
    st.session_state.pop("media", None)
    st.session_state["media_history"] = []
    st.session_state["upload_version"] += 1  # gives the uploader a fresh, empty state


def edit_image(media: dict, instruction: str) -> bytes:
    """Ask the image model to change the current image; returns PNG bytes."""
    extension = EDITABLE_IMAGES[media["mime"]]
    result = client.images.edit(
        model=IMAGE_MODEL,
        image=(f"imagem.{extension}", media["data"], media["mime"]),
        prompt=f"{instruction} Mantém a composição e o resto da imagem iguais.",
    )
    return base64.b64decode(result.data[0].b64_json)


def apply_image_edit(instruction: str) -> None:
    """Edit the post image, keeping the previous one for 'Desfazer edição'."""
    media = st.session_state["media"]
    try:
        new_data = edit_image(media, instruction)
    except Exception as error:
        if "verif" in str(error).lower():
            st.session_state["image_error"] = VERIFICATION_HELP
        else:
            st.session_state["image_error"] = f"Não foi possível editar a imagem: {error}"
        return
    st.session_state["media_history"].append(media)
    st.session_state["media"] = {**media, "data": new_data, "mime": "image/png", "name": "postmind-imagem-editada.png"}


def undo_image_edit() -> None:
    st.session_state["media"] = st.session_state["media_history"].pop()


def use_idea(topic: str) -> None:
    """Fill the topic box with a suggested idea."""
    st.session_state["topic"] = topic
    st.session_state["source_mode"] = "Tema"


def set_cookie(name: str, value: str, days: int = 365) -> None:
    """Queue a browser cookie; it is written by a tiny script at the top of the next run."""
    st.session_state.setdefault("pending_cookies", {})[name] = (value, days)


def read_cookie(name: str) -> str | None:
    try:
        value = st.context.cookies.get(name)
    except Exception:  # no browser (e.g. tests)
        return None
    return value if isinstance(value, str) and value else None


def toggle_theme() -> None:
    st.session_state["dark"] = not st.session_state["dark"]
    set_cookie("pm_theme", "dark" if st.session_state["dark"] else "light")


def owner_key() -> str:
    """Who the drafts belong to: the account email, or this browser for visitors."""
    user = current_user()
    if user and user.get("email"):
        return user["email"].lower()
    return f"device:{st.session_state['device_id']}"


def claim_device_drafts() -> None:
    """After signing in, the drafts made in this browser move to the account."""
    user = current_user()
    if user and user.get("email") and not st.session_state.get("drafts_claimed"):
        drafts.move_owner(f"device:{st.session_state['device_id']}", user["email"].lower())
        st.session_state["drafts_claimed"] = True


def draft_signature() -> tuple:
    media = st.session_state.get("media")
    return (st.session_state.get("post_text", ""), hash(media["data"]) if media else None)


def show_draft(draft: dict | None) -> None:
    """Put a saved draft (or nothing) in the editor and preview."""
    for key in ("versions", "version_choice", "last_tool"):
        st.session_state.pop(key, None)
    st.session_state["post_history"] = []
    st.session_state["media_history"] = []
    if draft is None:
        for key in ("post_text", "media", "draft_id"):
            st.session_state.pop(key, None)
    else:
        st.session_state["post_text"] = draft["post_text"]
        st.session_state["draft_id"] = draft["id"]
        if draft["media"]:
            st.session_state["media"] = draft["media"]
        else:
            st.session_state.pop("media", None)
    st.session_state["saved_signature"] = draft_signature()


def open_draft(draft_id: int) -> None:
    show_draft(drafts.load(owner_key(), draft_id))


def delete_draft(draft_id: int) -> None:
    drafts.delete(owner_key(), draft_id)
    if st.session_state.get("draft_id") == draft_id:
        show_draft(None)


def autosave_draft() -> None:
    """Save the current post as a draft whenever it changes."""
    if not st.session_state.get("post_text", "").strip():
        return
    signature = draft_signature()
    if signature == st.session_state.get("saved_signature"):
        return
    st.session_state["draft_id"] = drafts.save(
        owner_key(),
        st.session_state.get("draft_id"),
        st.session_state["post_text"],
        st.session_state.get("media"),
    )
    st.session_state["saved_signature"] = signature


def time_ago(iso_time: str) -> str:
    seconds = (datetime.now(timezone.utc) - datetime.fromisoformat(iso_time)).total_seconds()
    if seconds < 60:
        return "agora mesmo"
    if seconds < 3600:
        return f"há {int(seconds // 60)} min"
    if seconds < 86400:
        return f"há {int(seconds // 3600)} h"
    return f"há {int(seconds // 86400)} dias"


def start_session() -> None:
    """Once per browser session: restore login, theme and the last post from cookies/drafts."""
    if st.session_state.get("started"):
        return
    st.session_state["started"] = True

    device_id = read_cookie("pm_device")
    if not device_id:
        device_id = uuid.uuid4().hex
        set_cookie("pm_device", device_id, days=3650)
    st.session_state["device_id"] = device_id

    st.session_state["dark"] = read_cookie("pm_theme") == "dark"

    account = accounts.account_for_session(read_cookie("pm_session"))
    if account and not st.user.is_logged_in:
        st.session_state["account"] = {**account, "picture": None}

    claim_device_drafts()
    show_draft(drafts.load(owner_key()))


def signed_in_with_email(account: dict) -> None:
    """Keep the email login for 30 days, move this browser's drafts to the account."""
    st.session_state["account"] = {**account, "picture": None}
    set_cookie("pm_session", accounts.create_session(account["email"]), days=accounts.SESSION_DAYS)
    claim_device_drafts()
    if not st.session_state.get("post_text"):
        show_draft(drafts.load(owner_key()))


def login_configured(provider: str) -> bool:
    """True when .streamlit/secrets.toml has a client_id for this provider."""
    try:
        return bool(st.secrets["auth"][provider]["client_id"])
    except Exception:  # no secrets file or missing section
        return False


def current_user() -> dict | None:
    """The signed-in user ({'name', 'email', 'picture'}), from Google/LinkedIn or email."""
    if st.user.is_logged_in:
        return {
            "name": st.user.get("name") or "",
            "email": st.user.get("email") or "",
            "picture": st.user.get("picture"),
        }
    return st.session_state.get("account")


def sign_out() -> None:
    if st.user.is_logged_in:
        st.logout()
    else:
        accounts.end_session(read_cookie("pm_session"))
        set_cookie("pm_session", "", days=-1)
        st.session_state.pop("account", None)
        st.session_state["drafts_claimed"] = False
        show_draft(None)
        st.rerun()


def auth_header(title: str, text: str) -> None:
    st.html(
        f"""
        <div class="pm-login-head">
          <div class="pm-logo">Pm</div>
          <div><b>{title}</b><small>{text}</small></div>
        </div>
        """
    )


def social_buttons(verb: str) -> None:
    """'Continue with Google / LinkedIn' buttons."""
    for provider, name in LOGIN_PROVIDERS.items():
        with st.container(key=f"login_{provider}"):
            if st.button(f"{verb} com {name}", width="stretch"):
                if login_configured(provider):
                    st.login(provider)
                else:
                    st.warning(
                        f"O login com {name} ainda não está configurado. "
                        "Falta preencher as chaves em `.streamlit/secrets.toml`."
                    )
    st.html('<div class="pm-or"><span>ou com o teu email</span></div>')


@st.dialog("Entrar")
def sign_in_dialog() -> None:
    auth_header("Bem-vindo de volta", "Entra na tua conta PostMind.")
    social_buttons("Entrar")
    with st.form("sign_in_form", border=False):
        email = st.text_input("Email", placeholder="nome@empresa.pt")
        password = st.text_input("Palavra-passe", type="password")
        submitted = st.form_submit_button("Entrar", type="primary", width="stretch")
    if submitted:
        account = accounts.check_login(email, password)
        if account:
            signed_in_with_email(account)
            st.rerun()
        else:
            st.error("Email ou palavra-passe incorretos.")
    st.html(
        '<div class="pm-login-foot">Ainda não tens conta? '
        "Usa <b>Criar conta</b> no topo da página.</div>"
    )


@st.dialog("Criar conta")
def sign_up_dialog() -> None:
    auth_header("Cria a tua conta PostMind", "Grátis e em segundos.")
    social_buttons("Registar")
    with st.form("sign_up_form", border=False):
        name = st.text_input("Nome", placeholder="O teu nome")
        email = st.text_input("Email", placeholder="nome@empresa.pt")
        password = st.text_input(
            "Palavra-passe",
            type="password",
            placeholder=f"Mínimo {accounts.MIN_PASSWORD_LENGTH} caracteres",
        )
        confirm = st.text_input("Confirmar palavra-passe", type="password")
        submitted = st.form_submit_button("Criar conta", type="primary", width="stretch")
    if submitted:
        error = accounts.create_account(name, email, password, confirm)
        if error:
            st.error(error)
        else:
            signed_in_with_email({"name": name.strip(), "email": email.strip().lower()})
            st.rerun()
    st.html(
        '<div class="pm-login-foot">A tua palavra-passe é guardada cifrada. '
        "Já tens conta? Usa <b>Entrar</b> no topo da página.</div>"
    )


def avatar_html(css_class: str) -> str:
    """The signed-in user's photo, or initials when there is none."""
    user = current_user()
    if user and user.get("picture"):
        return f'<img class="{css_class}" src="{html.escape(user["picture"])}" alt="">'
    name = user["name"] if user else ""
    initials = "".join(part[0] for part in name.split()[:2]).upper() or "EU"
    return f'<div class="{css_class}">{html.escape(initials)}</div>'


def post_header_html() -> str:
    author = (current_user() or {}).get("name") or "O teu nome"
    return f"""
    <div class="li-post-header">
      {avatar_html("li-avatar")}
      <div>
        <div class="li-name">{html.escape(author)} <span class="li-degree">· Tu</span></div>
        <div class="li-headline">O teu cargo na empresa</div>
        <div class="li-meta">Agora · {icon("public")}</div>
      </div>
      <div class="li-follow">+ Seguir</div>
    </div>
    """


def phone_top_html() -> str:
    """Status bar + LinkedIn app bar at the top of the phone preview."""
    return f"""
    <div class="pm-phone-top">
      <div class="pm-phone-status">
        <b>9:41</b>
        <span>{icon("signal_cellular_alt")}{icon("wifi")}{icon("battery_full")}</span>
      </div>
      <div class="pm-phone-appbar">
        {avatar_html("pm-phone-avatar")}
        <div class="pm-phone-search">{icon("search")} Pesquisar</div>
        {icon("chat")}
      </div>
    </div>
    """


def phone_next_html() -> str:
    """The next post peeking in the feed, under the user's post."""
    return """
    <div class="pm-phone-next">
      <div class="pm-skeleton">
        <div class="pm-skel-circle"></div>
        <div class="pm-skel-lines">
          <div class="pm-skel-line" style="width:45%"></div>
          <div class="pm-skel-line" style="width:30%"></div>
        </div>
      </div>
      <div class="pm-skel-line"></div>
      <div class="pm-skel-line" style="width:85%"></div>
    </div>
    """


def phone_nav_html() -> str:
    """The LinkedIn app's bottom navigation."""
    tabs = [("home", "Início"), ("group", "Rede"), ("add_box", "Publicar"),
            ("notifications", "Notificações"), ("work", "Empregos")]
    items = "".join(
        f'<div class="{"active" if label == "Início" else ""}">{icon(name)}<span>{label}</span></div>'
        for name, label in tabs
    )
    return f'<div class="pm-phone-nav">{items}</div>'


def write_post_live(job: dict, live) -> None:
    """Write the post into the preview word by word, then store it (and the image)."""
    angles = list(ANGLES.values()) if job["three_versions"] else [""]
    others = []
    if job["three_versions"]:  # versions 2 and 3 are written in the background meanwhile
        pool = ThreadPoolExecutor(max_workers=2)
        others = [
            pool.submit(generate_post, job["topic"], job["tone"], angle, job["source"])
            for angle in angles[1:]
        ]

    post, last_draw = "", 0.0
    for piece in stream_post(job["topic"], job["tone"], angles[0], job["source"]):
        post += piece
        if time.monotonic() - last_draw > 0.05:  # redraw ~20 times per second
            live.html(post_header_html() + f'<div class="li-text">{post_to_html(post)}<span class="pm-cursor"></span></div>')
            last_draw = time.monotonic()
    live.html(post_header_html() + f'<div class="li-text">{post_to_html(post)}</div>')

    if job["three_versions"]:
        with st.spinner("A terminar as outras 2 versões..."):
            versions = [post] + [future.result() for future in others]
        pool.shutdown()
        st.session_state["versions"] = versions
        st.session_state["version_choice"] = list(ANGLES)[0]
    else:
        st.session_state.pop("versions", None)
        st.session_state.pop("version_choice", None)
    st.session_state["post_text"] = post
    st.session_state["post_history"] = []
    st.session_state.pop("draft_id", None)  # a new post becomes a new draft

    if job["media_mode"] == "Sem imagem":
        st.session_state.pop("media", None)
    elif job["with_ai_image"]:
        with st.spinner("A criar a imagem para o teu post... pode demorar até um minuto."):
            make_ai_image(f"{job['topic'].strip()} {post[:300]}".strip(), job["image_style"])


def fold_index(text: str, device: str = "Computador") -> int:
    """Where LinkedIn's '...ver mais' cuts the post (len(text) when it shows everything)."""
    cut = min(len(text), DEVICE_MAX_CHARS[device])
    line_breaks = [i for i, char in enumerate(text) if char == "\n"]
    if len(line_breaks) >= PREVIEW_MAX_LINES:
        cut = min(cut, line_breaks[PREVIEW_MAX_LINES - 1])
    return cut


def fold_marker_html(text: str, device: str) -> str:
    """Panel showing what readers see before '...ver mais' and whether the hook fits."""
    cut = fold_index(text, device)
    visible, hidden = text[:cut], text[cut:]
    first_line = next((line for line in text.splitlines() if line.strip()), "")
    hook_end = text.find(first_line) + len(first_line)

    if not hidden.strip():
        state, symbol, message = "ok", "check_circle", "O post é curto: aparece inteiro, sem «ver mais»."
    elif hook_end <= cut:
        state, symbol, message = "ok", "check_circle", (
            f"O gancho cabe inteiro antes do «ver mais» ({len(first_line)} caracteres)."
        )
    else:
        state, symbol, message = "warn", "warning", (
            f"A primeira frase é cortada pelo «ver mais» ({len(first_line)} caracteres). "
            "Encurta-a ou usa «Novo gancho»."
        )

    cut_mark = f'<span class="pm-fold-cut">{icon("content_cut")} …ver mais</span>' if hidden.strip() else ""
    rest = post_to_html(hidden[:220]) + ("…" if len(hidden) > 220 else "")
    return f"""
    <div class="pm-fold">
      <div class="pm-fold-head">{icon("visibility")} O que aparece antes do «ver mais» · {device.lower()}</div>
      <div class="pm-fold-text"><span class="pm-fold-visible">{post_to_html(visible)}</span>{cut_mark}<span class="pm-fold-hidden">{rest}</span></div>
      <div class="pm-fold-status {state}">{icon(symbol)} {message}</div>
    </div>
    """


def post_to_html(text: str) -> str:
    """Escape the post and highlight hashtags in LinkedIn blue."""
    safe = html.escape(text, quote=False)
    return re.sub(r"#(\w+)", r'<span class="hashtag">#\1</span>', safe)


def icon(name: str) -> str:
    """HTML for a Material Symbols icon."""
    return f'<span class="pm-icon">{name}</span>'


def copy_button(text: str) -> None:
    """Render a button that copies the text to the clipboard."""
    copy_label = f"{icon('content_copy')} Copiar texto"
    done_label = f"{icon('check')} Copiado!"
    st.html(
        f"""
        <button id="copy-post">{copy_label}</button>
        <script>
          (() => {{
            const text = {json.dumps(text)};
            const button = document.getElementById("copy-post");
            button.onclick = async () => {{
              try {{
                await navigator.clipboard.writeText(text);
              }} catch (e) {{
                const area = document.createElement("textarea");
                area.value = text;
                document.body.appendChild(area);
                area.select();
                document.execCommand("copy");
                area.remove();
              }}
              button.innerHTML = {json.dumps(done_label)};
              setTimeout(() => (button.innerHTML = {json.dumps(copy_label)}), 2000);
            }};
          }})();
        </script>
        """,
        unsafe_allow_javascript=True,
    )


# --- Page setup ---
st.set_page_config(page_title="PostMind", page_icon="✍️", layout="wide")
st.session_state.setdefault("dark", False)
st.session_state.setdefault("upload_version", 0)
st.session_state.setdefault("post_history", [])
st.session_state.setdefault("media_history", [])
start_session()

css = (BASE_DIR / "styles.css").read_text(encoding="utf-8")
if st.session_state["dark"]:
    css += (BASE_DIR / "dark.css").read_text(encoding="utf-8")
st.html(f"<style>{css}</style>")

# Streamlit marks every page as English; say it is pt-PT so browsers stop offering to translate it
with st.container(key="page_language"):
    st.html(
        """
        <script>
          const root = document.documentElement;
          root.lang = "pt-PT";
          root.setAttribute("translate", "no");
          if (!document.querySelector('meta[name="google"]')) {
            const meta = document.createElement("meta");
            meta.name = "google";
            meta.content = "notranslate";
            document.head.appendChild(meta);
          }
        </script>
        """,
        unsafe_allow_javascript=True,
    )

pending_cookies = st.session_state.pop("pending_cookies", None)
if pending_cookies:
    with st.container(key="cookie_jar"):
        st.html(
            "<script>"
            + "".join(
                f"document.cookie = {json.dumps(f'{name}={value}; path=/; max-age={days * 86400}; SameSite=Lax')};"
                for name, (value, days) in pending_cookies.items()
            )
            + "</script>",
            unsafe_allow_javascript=True,
        )

# --- Top navigation bar ---
st.html(
    f"""
    <nav class="pm-nav"><div class="pm-nav-inner">
      <div class="pm-brand">
        <div class="pm-logo">Pm</div>PostMind<span class="pm-beta">Beta</span>
      </div>
    </div></nav>
    """
)
with st.container(key="nav_actions", horizontal=True, vertical_alignment="center", gap="small"):
    user = current_user()
    if user:
        first_name = (user["name"] or "Conta").split()[0]
        st.html(
            f'<div class="pm-user">{avatar_html("pm-user-avatar")}'
            f"<span>{html.escape(first_name)}</span></div>"
        )
        if st.button("Sair", icon=":material/logout:", type="tertiary", key="logout"):
            sign_out()
    else:
        if st.button("Entrar", key="sign_in"):
            sign_in_dialog()
        if st.button("Criar conta", key="sign_up"):
            sign_up_dialog()
    dark = st.session_state["dark"]
    st.button(
        "Claro" if dark else "Escuro",
        icon=":material/light_mode:" if dark else ":material/dark_mode:",
        key="theme",
        on_click=toggle_theme,
    )

left, center, right = st.columns([1.3, 2.2, 1.25], gap="medium")

# --- Left: quick AI tools ---
with left:
    has_post = bool(st.session_state.get("post_text", "").strip())
    with st.container(key="tools"):
        subtitle = "Reescreve o teu post num clique" if has_post else "Gera um post primeiro"
        st.html(
            f"""
            <div class="pm-card-title">Ferramentas IA {icon("auto_fix_high")}</div>
            <div class="pm-card-sub">{subtitle}</div>
            """
        )
        chosen = None
        for tool_icon, label, instruction in QUICK_TOOLS:
            if st.button(
                label,
                icon=f":material/{tool_icon}:",
                key=f"tool_{label}",
                disabled=not has_post,
                width="stretch",
            ):
                chosen = (label, instruction)

        with st.form("custom_tool", border=False, clear_on_submit=True):
            custom = st.text_input(
                "Outra alteração",
                placeholder="Pede outra alteração…",
                label_visibility="collapsed",
                disabled=not has_post,
            )
            if st.form_submit_button(
                "Aplicar", icon=":material/send:", width="stretch", disabled=not has_post
            ) and custom.strip():
                chosen = ("Pedido personalizado", custom.strip())

        if chosen:
            label, instruction = chosen
            with st.spinner(f"A aplicar «{label}»..."):
                try:
                    new_post = rewrite_post(st.session_state["post_text"], instruction)
                    st.session_state["post_history"].append(st.session_state["post_text"])
                    st.session_state["post_text"] = new_post
                    st.session_state["last_tool"] = label
                    st.rerun()
                except Exception as error:
                    st.error(f"Não foi possível alterar o post: {error}")

        if st.session_state["post_history"]:
            last_tool = st.session_state.get("last_tool")
            status_col, undo_col = st.columns([3, 2], vertical_alignment="center")
            with status_col:
                status = f"Aplicado: {html.escape(last_tool)}" if last_tool else "Versão reposta"
                st.html(f'<div class="pm-tool-status">{icon("check_circle")} {status}</div>')
            with undo_col:
                st.button(
                    "Desfazer", icon=":material/undo:", type="tertiary", key="undo", on_click=undo_rewrite
                )

# --- Center: composer + preview ---
with center:
    with st.container(key="composer"):
        st.html(
            f"""
            <div class="pm-composer-head">
              <div class="pm-badge-icon">{icon("edit_note")}</div>
              <div>
                <div class="pm-composer-title">Começar um post</div>
                <div class="pm-composer-sub">Sobre o que queres publicar hoje?</div>
              </div>
            </div>
            """
        )
        source_mode = st.segmented_control(
            "Fonte",
            SOURCE_MODES,
            key="source_mode",
            default="Tema",
            format_func=lambda mode: {
                "Tema": ":material/edit: Tema",
                "Link": ":material/link: Link",
                "Documento": ":material/description: Documento",
            }[mode],
            label_visibility="collapsed",
        ) or "Tema"
        source_url, source_document = "", None
        if source_mode == "Link":
            source_url = st.text_input(
                "Link",
                key="source_url",
                placeholder="Cola o link de uma notícia ou artigo · https://…",
                label_visibility="collapsed",
            )
        elif source_mode == "Documento":
            source_document = st.file_uploader(
                "Documento",
                type=sources.DOCUMENT_TYPES,
                key="source_document",
                label_visibility="collapsed",
            )
        topic = st.text_area(
            "Tema",
            key="topic",
            placeholder=(
                "Ex.: a nossa empresa lançou um chatbot para restaurantes"
                if source_mode == "Tema"
                else "O que queres destacar? (opcional)"
            ),
            height=110 if source_mode == "Tema" else 70,
            label_visibility="collapsed",
        )
        st.html('<div class="pm-label">Tom do post</div>')
        tone = st.segmented_control(
            "Tom",
            list(TONES.keys()),
            key="tone",
            default="Profissional",
            label_visibility="collapsed",
        )
        st.html('<div class="pm-label">Imagem</div>')
        media_mode = st.segmented_control(
            "Imagem",
            MEDIA_MODES,
            key="media_mode",
            default="Gerar com IA",
            label_visibility="collapsed",
        ) or "Sem imagem"
        if media_mode == "Gerar com IA":
            image_style = st.segmented_control(
                "Estilo da imagem",
                list(IMAGE_STYLES.keys()),
                key="image_style",
                default="Fotografia",
                label_visibility="collapsed",
            ) or "Fotografia"
        elif media_mode == "Anexar ficheiro":
            upload_key = f"upload_{st.session_state['upload_version']}"
            st.file_uploader(
                "Ficheiro",
                type=MEDIA_TYPES,
                key=upload_key,
                on_change=store_upload,
                args=(upload_key,),
                label_visibility="collapsed",
            )

        with_ai_image = media_mode == "Gerar com IA"
        hint_col, button_col = st.columns([2, 3], vertical_alignment="center")
        with hint_col:
            with st.container(key="versions_toggle", horizontal=True, vertical_alignment="center", gap="small"):
                three_versions = st.toggle("3 versões", key="three_versions")
                with st.popover("", icon=":material/help:", type="tertiary", key="versions_help"):
                    st.html(
                        """
                        <div class="pm-help">
                          <b>3 versões do mesmo post</b>
                          <p>A IA escreve 3 versões ao mesmo tempo, cada uma com uma abordagem:</p>
                          <ul>
                            <li><b>História</b> · conta-o como uma pequena história</li>
                            <li><b>Direto</b> · frases curtas, sem rodeios</li>
                            <li><b>Em lista</b> · pontos claros e fáceis de ler</li>
                          </ul>
                          <p>Depois escolhes a que preferes por cima da pré-visualização.</p>
                        </div>
                        """
                    )
        with button_col:
            generate = st.button(
                "Gerar post + imagem" if with_ai_image else "Gerar post",
                icon=":material/auto_awesome:",
                type="primary",
                width="stretch",
            )

    job = None
    if generate:
        if source_mode == "Tema" and not topic.strip():
            st.warning("Escreve primeiro o tema do post.")
        else:
            try:
                job = {
                    "topic": topic,
                    "tone": tone or "Profissional",
                    "source": build_source(source_mode, source_url, source_document),
                    "three_versions": three_versions,
                    "media_mode": media_mode,
                    "with_ai_image": with_ai_image,
                    "image_style": image_style if with_ai_image else None,
                }
            except ValueError as error:  # friendly messages from build_source / sources
                st.warning(str(error))
            except Exception as error:
                st.error(f"Ocorreu um erro ao ler a fonte: {error}")

    image_error = st.session_state.pop("image_error", None)
    if image_error:
        st.error(image_error)

    with st.container(key="preview_bar", horizontal=True, vertical_alignment="center"):
        st.html('<div class="pm-divider"><b>Pré-visualização</b> do teu post</div>')
        device = st.segmented_control(
            "Dispositivo",
            list(DEVICE_MAX_CHARS),
            key="device",
            default="Computador",
            format_func=lambda name: {
                "Computador": ":material/computer: Computador",
                "Telemóvel": ":material/smartphone: Telemóvel",
            }[name],
            label_visibility="collapsed",
        ) or "Computador"

    if st.session_state.get("versions"):
        with st.container(key="version_picker", horizontal=True, vertical_alignment="center"):
            st.html(f'<div class="pm-label">{icon("layers")} Escolhe a versão</div>')
            st.segmented_control(
                "Versão",
                list(ANGLES),
                key="version_choice",
                on_change=choose_version,
                label_visibility="collapsed",
            )

    with st.container(key="preview"):
        if job:
            try:
                write_post_live(job, st.empty())
                st.rerun()  # show the finished post with all its tools
            except Exception as error:
                st.error(f"Ocorreu um erro ao gerar o post: {error}")
        media = st.session_state.get("media")
        if "post_text" in st.session_state or media:
            post = st.session_state.get("post_text", "")
            clamp = "clamp" if fold_index(post, device) < len(post.rstrip()) else ""
            see_more = '<label for="li-more" class="li-more">…ver mais</label>' if clamp else ""

            preview_tab, edit_tab = st.tabs(
                [":material/visibility: Pré-visualização", ":material/edit: Editar texto"]
            )
            with preview_tab:
                on_phone = device == "Telemóvel"
                with st.container(key="phone" if on_phone else "desktop"):
                    if on_phone:
                        st.html(phone_top_html())
                    st.html(
                        post_header_html()
                        + f"""
                        <input type="checkbox" id="li-more" class="li-more-toggle">
                        <div class="li-text {clamp}">{post_to_html(post)}</div>
                        {see_more}
                        """
                    )
                    if media:
                        if media["mime"].startswith("video/"):
                            st.video(media["data"])
                        else:
                            st.image(media["data"], width="stretch")
                    st.html(
                        """
                        <div class="li-reactions">
                          <span class="li-bubbles"><span>👍</span><span>❤️</span><span>👏</span></span>
                          Tu e outras 127 pessoas
                          <span class="li-right">14 comentários · 6 partilhas</span>
                        </div>
                        """
                    )
                    if on_phone:
                        st.html(phone_next_html())
                        st.html(phone_nav_html())
                if media:
                    new_image = False
                    with st.container(key="media_actions"):
                        download_col, new_col, remove_col = st.columns(3)
                        with download_col:
                            st.download_button(
                                "Descarregar",
                                data=media["data"],
                                file_name=media["name"],
                                mime=media["mime"],
                                icon=":material/download:",
                                type="tertiary",
                            )
                        with new_col:
                            if media["source"] == "ai":
                                new_image = st.button(
                                    "Nova imagem", icon=":material/refresh:", type="tertiary"
                                )
                        with remove_col:
                            st.button(
                                "Remover", icon=":material/delete:", type="tertiary", on_click=remove_media
                            )
                    if new_image:
                        with st.spinner("A criar uma nova imagem..."):
                            make_ai_image(*st.session_state["image_request"])
                        st.rerun()

                    if media["mime"] in EDITABLE_IMAGES:
                        edit_request = None
                        with st.container(key="image_edit"):
                            st.html(f'<div class="pm-label">{icon("auto_fix_high")} Editar imagem com IA</div>')
                            with st.container(key="image_chips", horizontal=True, gap="small"):
                                for label, instruction in QUICK_IMAGE_EDITS:
                                    if st.button(label, key=f"image_edit_{label}"):
                                        edit_request = (label, instruction)
                            with st.form("image_edit_form", border=False, clear_on_submit=True):
                                input_col, button_col = st.columns([3, 1.2], vertical_alignment="bottom")
                                with input_col:
                                    custom_edit = st.text_input(
                                        "Edição",
                                        placeholder="Ex.: põe o fundo em Lisboa",
                                        label_visibility="collapsed",
                                    )
                                with button_col:
                                    if st.form_submit_button("Editar", width="stretch") and custom_edit.strip():
                                        edit_request = ("Edição", custom_edit.strip())
                            if st.session_state["media_history"]:
                                done_col, undo_col = st.columns([3, 2], vertical_alignment="center")
                                with done_col:
                                    st.html(f'<div class="pm-tool-status">{icon("check_circle")} Imagem editada</div>')
                                with undo_col:
                                    st.button(
                                        "Desfazer edição",
                                        icon=":material/undo:",
                                        type="tertiary",
                                        key="undo_image",
                                        on_click=undo_image_edit,
                                    )
                        if edit_request:
                            with st.spinner(f"A editar a imagem («{edit_request[0]}»)... pode demorar até um minuto."):
                                apply_image_edit(edit_request[1])
                            st.rerun()

            with edit_tab:
                st.text_area(
                    "Texto do post",
                    key="post_text",
                    height=320,
                    label_visibility="collapsed",
                )
                if post:
                    st.html(fold_marker_html(post, device))

            if post:
                percent = min(100, len(post) * 100 // LINKEDIN_MAX_CHARS)
                over = "over" if len(post) > LINKEDIN_MAX_CHARS else ""
                st.html(
                    f"""
                    <div class="pm-counter">
                      {len(post)} / {LINKEDIN_MAX_CHARS} caracteres
                      <div class="pm-counter-bar"><div class="{over}" style="width:{percent}%"></div></div>
                    </div>
                    """
                )

                copy_col, linkedin_col = st.columns(2)
                with copy_col:
                    copy_button(post)
                with linkedin_col:
                    st.link_button(
                        "Publicar no LinkedIn",
                        "https://www.linkedin.com/feed/?shareActive=true&text=" + quote(post),
                        icon=":material/open_in_new:",
                        width="stretch",
                    )
                if media:
                    st.html(
                        f'<div class="pm-hint">{icon("info")} O link do LinkedIn só leva o texto: '
                        "descarrega a imagem e anexa-a à mão.</div>"
                    )
        else:
            st.html(
                """
                <div class="pm-skeleton">
                  <div class="pm-skel-circle"></div>
                  <div class="pm-skel-lines">
                    <div class="pm-skel-line" style="width:40%"></div>
                    <div class="pm-skel-line" style="width:25%"></div>
                  </div>
                </div>
                <div class="pm-skel-line"></div>
                <div class="pm-skel-line" style="width:92%"></div>
                <div class="pm-skel-line" style="width:70%"></div>
                <div class="pm-empty-text">O teu post vai aparecer aqui, tal como no LinkedIn.</div>
                """
            )

# --- Right: ideas + footer ---
with right:
    with st.container(key="ideas"):
        st.html(
            f"""
            <div class="pm-ideas-head">
              <div class="pm-card-title">Ideias para posts {icon("lightbulb")}</div>
              <div class="pm-card-sub">Clica numa ideia para preencher o tema</div>
            </div>
            """
        )
        for icon_name, title, idea_topic in IDEAS:
            st.button(
                title,
                key=f"idea_{title}",
                icon=f":material/{icon_name}:",
                help=idea_topic,
                type="tertiary",
                on_click=use_idea,
                args=(idea_topic,),
            )

    autosave_draft()
    signed_in = bool(current_user())
    with st.container(key="drafts"):
        head_col, new_col = st.columns([3, 2], vertical_alignment="center")
        with head_col:
            st.html(f'<div class="pm-card-title">Rascunhos {icon("history")}</div>')
        with new_col:
            st.button("Novo", icon=":material/add:", type="tertiary", key="new_post", on_click=show_draft, args=(None,))
        st.html(
            '<div class="pm-card-sub">'
            + ("Guardados na tua conta." if signed_in else "Guardados neste browser · entra para os teres em qualquer lado.")
            + "</div>"
        )
        saved = drafts.recent(owner_key())
        if not saved:
            st.html('<div class="pm-drafts-empty">Os posts que gerares ficam guardados aqui automaticamente.</div>')
        for draft in saved:
            first_line = next((line for line in draft["post_text"].splitlines() if line.strip()), "Sem texto")
            title = first_line[:38] + ("…" if len(first_line) > 38 else "")
            active = draft["id"] == st.session_state.get("draft_id")
            with st.container(key=f"draft_{draft['id']}{'_active' if active else ''}", horizontal=True, vertical_alignment="center", gap="small"):
                st.button(
                    title,
                    key=f"open_draft_{draft['id']}",
                    help=time_ago(draft["updated_at"]) + (" · com imagem" if draft["has_media"] else ""),
                    type="tertiary",
                    icon=":material/image:" if draft["has_media"] else ":material/article:",
                    on_click=open_draft,
                    args=(draft["id"],),
                )
                st.button(
                    "",
                    key=f"delete_draft_{draft['id']}",
                    icon=":material/delete:",
                    type="tertiary",
                    help="Apagar rascunho",
                    on_click=delete_draft,
                    args=(draft["id"],),
                )

    st.html(
        """
        <div class="pm-footer">
          <b>PostMind</b> © 2026<br>
          Feito com Streamlit e OpenAI · Demo interna
        </div>
        """
    )
