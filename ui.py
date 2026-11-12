"""DineVoice kiosk UI. Run: python3 -m streamlit run ui.py"""

import os
import streamlit as st
from dotenv import load_dotenv
from app import (
    MENU,
    new_session,
    record_audio,
    transcribe,
    respond,
    speak,
    get_bill,
    simulate_payment,
    generate_receipt,
)
import subprocess
import tempfile

load_dotenv()
st.set_page_config(page_title="DineVoice Kiosk", page_icon="🍽️", layout="wide")
st.markdown(
    """<style>
.stApp {background:#f4f8f7;color:#172b36}
.block-container {padding-top:1.3rem;max-width:1300px}
.stApp h1,.stApp h2,.stApp h3 {color:#116b46!important}
.stApp [data-testid="stMarkdownContainer"] p,.stApp label p {color:#243746}
.stApp [data-testid="stCaptionContainer"] p {color:#536575!important}
[data-testid="stMetric"] {background:#e2effd;border:1px solid #bdd8f4;border-radius:12px;padding:14px}
[data-testid="stMetricLabel"] p,[data-testid="stMetricValue"] {color:#124f88!important}
.stApp input {background:#fff!important;color:#172b36!important}
.stApp input::placeholder {color:#708391!important}
.stButton button[kind="primary"] {background:#ef861c!important;border-color:#ef861c!important;color:#fff!important}
.stButton button[kind="primary"] p {color:#fff!important}
.stButton button[kind="secondary"] {background:#fff!important;color:#183c54!important;border-color:#b8c9d3!important}
.stButton button[kind="secondary"] p {color:#183c54!important}
[data-testid="stChatMessage"] {background:#fff;border:1px solid #dce6ea}
</style>""",
    unsafe_allow_html=True,
)

if "order_session" not in st.session_state:
    st.session_state.order_session = new_session()
for key, default in {
    "last_audio": None,
    "welcome_audio": None,
    "welcome_attempted": False,
    "audio_serial": 0,
    "welcome_played": False,
}.items():
    if key not in st.session_state:
        st.session_state[key] = default
session = st.session_state.order_session


def money(amount):
    return f"S${amount:.2f}"


def reset():
    st.session_state.order_session = new_session()
    st.session_state.last_audio = None
    st.session_state.welcome_played = False
    # Reuse cached welcome audio; do not call ElevenLabs again unnecessarily.


def play_audio(audio, key):
    # Autoplay is a browser request; browsers may block it without user interaction.
    st.audio(audio, format="audio/mp3", autoplay=True)


if "welcome_initialized" not in st.session_state:
    st.session_state.welcome_initialized = False

if not st.session_state.welcome_initialized:
    st.session_state.welcome_initialized = True

    greeting = (
        "Welcome to DineVoice! "
        "Take a look at our menu. "
        "What would you like to order today?"
    )

    try:
        audio_bytes = speak(greeting)

        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as audio_file:
            audio_file.write(audio_bytes)
            audio_path = audio_file.name

        # Play through macOS speakers, bypassing browser autoplay.
        subprocess.Popen(
            ["afplay", audio_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    except Exception as exc:
        st.warning(f"Greeting playback failed: {exc}")

st.title("🍽️ DineVoice")
st.caption("Voice ordering · Gemini + ElevenLabs · Demo kiosk")
left, right = st.columns([1.1, 1], gap="large")
with left:
    st.subheader("🟢 Restaurant Menu")
    st.caption("Prices in SGD")
    for name, price in MENU.items():
        a, b = st.columns([3, 1])
        a.write(f"**{name}**")
        b.write(money(price))
    st.divider()
    st.subheader("🎤 Talk to DineVoice")
    if session["status"] == "ordering":
        st.info("Speak for 6 seconds or type your order below.")
        typed = st.text_input(
            "Type your order",
            placeholder="I'd like one cheese wrap...",
            key="typed_order",
        )
        a, b = st.columns(2)
        record = a.button(
            "🎙️ Record 6 seconds", type="primary", use_container_width=True
        )
        send = b.button("Send typed order", use_container_width=True)
        if record or send:
            path = None
            try:
                if record:
                    with st.spinner("Listening..."):
                        path = record_audio(seconds=6)
                        customer = transcribe(path)
                else:
                    customer = typed.strip()
                if not customer or (
                    customer.startswith("[") and customer.endswith("]")
                ):
                    st.warning("No clear speech detected. Please try again.")
                else:
                    with st.spinner("DineVoice is responding..."):
                        answer = respond(customer, session)
                        try:
                            st.session_state.last_audio = speak(answer)
                        except Exception as exc:
                            st.session_state.last_audio = None
                            st.warning(f"Voice generation unavailable: {exc}")
                        st.session_state.welcome_played = True
                    st.rerun()
            except Exception as exc:
                st.error(f"Request failed: {exc}")
            finally:
                if path and os.path.exists(path):
                    os.remove(path)
    elif session["status"] == "checkout":
        st.info("Review your bill and complete the demo payment.")
    else:
        st.success("Demo payment complete.")

    st.markdown("**🔊 DineVoice audio**")
    if st.session_state.last_audio:
        play_audio(st.session_state.last_audio, "response")
    elif st.session_state.welcome_audio and not st.session_state.welcome_played:
        play_audio(st.session_state.welcome_audio, "welcome")
    elif st.session_state.welcome_audio and st.button("🔊 Replay welcome greeting"):
        play_audio(st.session_state.welcome_audio, "welcome_replay")
    elif not st.session_state.welcome_audio:
        st.caption("Welcome audio unavailable; check your ElevenLabs API key.")

    st.divider()
    st.subheader("💬 Conversation")
    if session["messages"]:
        for message in session["messages"][-12:]:
            with st.chat_message(
                "assistant" if message["role"] == "assistant" else "user"
            ):
                st.write(message["content"])
    else:
        st.caption("Your conversation appears here after the first order.")

with right:
    st.subheader("🔵 Your Live Order")
    bill = get_bill(session["cart"])
    if not bill["items"]:
        st.info("Your cart is empty. Order from the menu on the left.")
    for item in bill["items"]:
        a, b = st.columns([3, 1])
        a.write(f"**{item['quantity']} × {item['name']}**")
        b.write(money(item["subtotal"]))
    st.divider()
    st.metric("Total (SGD)", money(bill["total"]))
    if session["status"] == "ordering":
        if st.button(
            "🟠 Review bill / Checkout",
            type="primary",
            disabled=not bool(bill["items"]),
            use_container_width=True,
        ):
            session["status"] = "checkout"
            session["awaiting_confirmation"] = False
            st.session_state.last_audio = None
            st.rerun()
    elif session["status"] == "checkout":
        st.subheader("🧾 Bill & Demo Payment")
        st.caption("SIMULATION ONLY — no real money is collected")
        for item in bill["items"]:
            st.write(f"{item['quantity']} × {item['name']} — {money(item['subtotal'])}")
        st.markdown(f"**Amount due: {money(bill['total'])}**")
        method = st.radio("Payment method", ["Card", "PayNow"], horizontal=True)
        if st.button(
            "🟢 Simulate successful payment", type="primary", use_container_width=True
        ):
            try:
                simulate_payment(session, method)
                st.session_state.last_audio = None
                st.rerun()
            except Exception as exc:
                st.error(f"Payment failed: {exc}")
        if st.button("← Back to ordering"):
            session["status"] = "ordering"
            session["awaiting_confirmation"] = False
            st.rerun()
    else:
        st.success("Demo payment successful!")
        st.metric("Pickup token", session["token"])
        st.write(f"Payment method: {session['payment_method']}")
        st.download_button(
            "📄 Download receipt",
            generate_receipt(session),
            file_name="dinevoice_receipt.txt",
            mime="text/plain",
        )
    st.divider()
    if st.button("🔄 Start new order", use_container_width=True):
        reset()
        st.rerun()
