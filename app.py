import os
import json
import tempfile
import uuid
from decimal import Decimal

import sounddevice as sd
import soundfile as sf

from dotenv import load_dotenv
from elevenlabs.client import ElevenLabs
from google import genai
from google.genai import types

load_dotenv()

# -------------------------
# API CLIENTS
# -------------------------

eleven = ElevenLabs(api_key=os.environ["ELEVENLABS_API_KEY"])

llm = genai.Client(
    api_key=os.environ["GEMINI_API_KEY"],
    http_options=types.HttpOptions(api_version="v1beta"),
)

VOICE_ID = "JBFqnCBsd6RMkjVDRZzb"
SAMPLE_RATE = 16000

# -------------------------
# RESTAURANT MENU
# -------------------------

MENU = {
    "Margherita Pizza": 12.00,
    "Veggie Burger": 9.50,
    "Cheese Wrap": 8.00,
    "French Fries": 4.00,
    "Lemonade": 3.00,
    "Iced Tea": 3.50,
}

SYSTEM = f"""
You are DineVoice, a friendly restaurant ordering assistant.

Menu in SGD:
{json.dumps(MENU)}

Rules:
- Only offer items on the menu.
- Ask for missing quantities.
- Handle order modifications and cancellations.
- Never invent items or prices.
- If allergies are mentioned, ask the customer to
  verify ingredients and cross-contact with staff.
- Keep spoken replies under 45 words.
- Suggest one available drink after a savoury order.
- If the customer declines, do not suggest again.
- Before checkout, repeat the order and total.
- Ask for explicit confirmation.
- Never claim that payment has already happened.
- After confirmation, direct the customer to checkout.
- Payment is simulated, not real.
- Do not claim the order was sent to the kitchen.
"""

# -------------------------
# ORDER STATE
# -------------------------


def new_session():
    return {
        "messages": [],
        "cart": {},
        "status": "ordering",
        "token": None,
        "payment_method": None,
    }


def calculate_total(cart):
    total = Decimal("0.00")

    for item, quantity in cart.items():
        if item in MENU and quantity > 0:
            total += Decimal(str(MENU[item])) * quantity

    return float(total.quantize(Decimal("0.01")))


def get_bill(cart):
    items = []

    for name, quantity in cart.items():
        if name not in MENU or quantity <= 0:
            continue

        price = Decimal(str(MENU[name]))
        subtotal = price * quantity

        items.append(
            {
                "name": name,
                "quantity": quantity,
                "unit_price": float(price),
                "subtotal": float(subtotal),
            }
        )

    return {
        "items": items,
        "total": calculate_total(cart),
        "currency": "SGD",
    }


# -------------------------
# AUDIO RECORDING
# -------------------------


def record_audio(seconds=6):
    print(f"Recording for {seconds} seconds...")

    audio = sd.rec(
        int(seconds * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="float32",
    )

    sd.wait()

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp:
        path = temp.name

    sf.write(path, audio, SAMPLE_RATE)

    return path


# -------------------------
# SPEECH TO TEXT
# -------------------------


def transcribe(path):
    with open(path, "rb") as audio_file:
        result = eleven.speech_to_text.convert(
            file=audio_file,
            model_id="scribe_v2",
        )

    return (result.text or "").strip()


# -------------------------
# GEMINI ORDER UNDERSTANDING
# -------------------------


def understand_order(user_text, session):
    """
    Gemini extracts structured order changes.
    Python validates all changes before applying them.
    """

    prompt = f"""
You are a restaurant order extraction engine.

Available menu:
{json.dumps(MENU)}

Current cart:
{json.dumps(session["cart"])}

Customer message:
{user_text}

Return only valid JSON:

{{
  "operations": [
    {{
      "action": "add",
      "item": "French Fries",
      "quantity": 1
    }}
  ],
  "finished": false,
  "confirmed": false
}}

Allowed actions:
- add: increase item quantity
- set: replace item quantity
- remove: remove item completely

Rules:
- Use exact menu item names.
- Quantities must be nonnegative integers.
- If the quantity is unclear, return no operation.
- If the customer corrects a previous quantity,
  use set instead of add.
- Do not invent menu items.
- finished means the customer has finished ordering.
- confirmed means explicit approval of the
  previously presented complete order.
- Do not mark confirmed for a generic "okay"
  unless it clearly approves the final order.
"""

    result = llm.models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
        ),
    )

    return json.loads(result.text)


def apply_operations(session, operations):
    cart = session["cart"]

    for operation in operations:
        item = operation.get("item")
        action = operation.get("action")
        quantity = operation.get("quantity")

        if item not in MENU:
            continue

        if type(quantity) is not int:
            continue

        if quantity < 0 or quantity > 50:
            continue

        if action == "add" and quantity > 0:
            cart[item] = cart.get(item, 0) + quantity

        elif action == "set":
            if quantity == 0:
                cart.pop(item, None)
            else:
                cart[item] = quantity

        elif action == "remove":
            cart.pop(item, None)


# -------------------------
# CONVERSATION ENGINE
# -------------------------


def respond(user_text, session):
    if session["status"] != "ordering":
        return "Your order is already at checkout."

    previous_cart = dict(session["cart"])

    extracted = understand_order(user_text, session)

    apply_operations(session, extracted.get("operations", []))

    bill = get_bill(session["cart"])

    session["messages"].append(
        {
            "role": "user",
            "content": user_text,
        }
    )

    # A changed cart invalidates earlier confirmation.
    if session["cart"] != previous_cart:
        session["awaiting_confirmation"] = False

    awaiting = session.get("awaiting_confirmation", False)

    confirmed = extracted.get("confirmed") is True and awaiting and bool(bill["items"])

    if confirmed:
        session["status"] = "checkout"
        session["awaiting_confirmation"] = False

        answer = (
            f"Your order is confirmed. "
            f"The total is {bill['total']:.2f} "
            f"Singapore dollars. "
            f"Please proceed to payment."
        )

    elif extracted.get("finished") and bill["items"]:
        session["awaiting_confirmation"] = True

        summary = ", ".join(
            f"{item['quantity']} {item['name']}" for item in bill["items"]
        )

        answer = (
            f"Your order includes {summary}. "
            f"The total is {bill['total']:.2f} "
            f"Singapore dollars. "
            f"Would you like to confirm?"
        )

    else:
        history = []

        for message in session["messages"]:
            role = "model" if message["role"] == "assistant" else "user"

            history.append(
                types.Content(
                    role=role,
                    parts=[types.Part.from_text(text=message["content"])],
                )
            )

        context = f"""
Current cart:
{json.dumps(session["cart"])}

Current total:
SGD {bill["total"]:.2f}

Awaiting final confirmation:
{session.get("awaiting_confirmation", False)}

Respond conversationally.
Do not invent prices or cart items.
"""

        result = llm.models.generate_content(
            model="gemini-2.5-flash",
            contents=history,
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM + context,
                temperature=0.2,
            ),
        )

        answer = result.text or ("Sorry, could you repeat that?")

    session["messages"].append(
        {
            "role": "assistant",
            "content": answer,
        }
    )

    return answer


# -------------------------
# ELEVENLABS TEXT TO SPEECH
# -------------------------


def speak(text):
    audio = eleven.text_to_speech.convert(
        text=text,
        voice_id=VOICE_ID,
        model_id="eleven_v4_turbo",
        output_format="mp3_44100_128",
    )

    audio_bytes = b"".join(audio)
    return audio_bytes


# -------------------------
# PAYMENT SIMULATION
# -------------------------


def simulate_payment(session, method):
    if session["status"] != "checkout":
        raise ValueError("Order must be confirmed before payment.")

    if method not in ("Card", "PayNow"):
        raise ValueError("Invalid payment method.")

    if not session["cart"]:
        raise ValueError("Cannot pay for an empty cart.")

    session["payment_method"] = method
    session["status"] = "paid"

    session["token"] = "DV-" + uuid.uuid4().hex[:6].upper()

    return {
        "success": True,
        "token": session["token"],
        "payment_method": method,
        "bill": get_bill(session["cart"]),
        "message": (
            "Demo payment successful. "
            "Please collect your order using "
            f"token {session['token']}."
        ),
    }


# -------------------------
# RECEIPT
# -------------------------


def generate_receipt(session):
    if session["status"] != "paid":
        raise ValueError("Payment not completed.")

    bill = get_bill(session["cart"])

    lines = [
        "DINEVOICE",
        "DEMO PAYMENT RECEIPT",
        "----------------------------",
        f"Token: {session['token']}",
        f"Payment: {session['payment_method']}",
        "",
    ]

    for item in bill["items"]:
        lines.append(
            f"{item['quantity']} x {item['name']}" f" - SGD {item['subtotal']:.2f}"
        )

    lines.extend(
        [
            "----------------------------",
            f"TOTAL: SGD {bill['total']:.2f}",
            "",
            "SIMULATED PAYMENT ONLY",
            "No real transaction occurred.",
            "Thank you for ordering!",
        ]
    )

    return "\n".join(lines)
