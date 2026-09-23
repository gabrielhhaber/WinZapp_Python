"""Native, screen-reader-friendly emoji picker shared by chat and Status."""

from __future__ import annotations

import unicodedata
from difflib import SequenceMatcher
from itertools import chain

import wx

from ui.dialogs.emoji_cldr_keywords_tr import EMOJI_CLDR_KEYWORDS_TR


# Categories follow the official Unicode/CLDR emoji ordering (Emoji 17.0,
# fully-qualified set); "frequent" stays a short curated shortcut row.
# Values are plain Unicode, so insertion needs no API conversion.
EMOJI_CATEGORIES = (
    ("emoji_category_frequent", "😀 😃 😄 😁 😂 😊 😍 🥰 😘 😎 😢 😭 😡 👍 👎 ❤️ 🎉 🙏 🔥"),
    ("emoji_category_smileys", "😀 😃 😄 😁 😆 😅 🤣 😂 🙂 🙃 🫠 😉 😊 😇 🥰 😍 🤩 😘 😗 ☺️ 😚 😙 🥲 😋 😛 😜 🤪 😝 🤑 🤗 🤭 🫢 🫣 🤫 🤔 🫡 🤐 🤨 😐 😑 😶 🫥 😶‍🌫️ 😏 😒 🙄 😬 😮‍💨 🤥 🫨 🙂‍↔️ 🙂‍↕️ 😌 😔 😪 🤤 😴 🫩 😷 🤒 🤕 🤢 🤮 🤧 🥵 🥶 🥴 😵 😵‍💫 🤯 🤠 🥳 🥸 😎 🤓 🧐 😕 🫤 😟 🙁 ☹️ 😮 😯 😲 😳 🫪 🥺 🥹 😦 😧 😨 😰 😥 😢 😭 😱 😖 😣 😞 😓 😩 😫 🥱 😤 😡 😠 🤬 😈 👿 💀 ☠️ 💩 🤡 👹 👺 👻 👽 👾 🤖 😺 😸 😹 😻 😼 😽 🙀 😿 😾 🙈 🙉 🙊 💌 💘 💝 💖 💗 💓 💞 💕 💟 ❣️ 💔 ❤️‍🔥 ❤️‍🩹 ❤️ 🩷 🧡 💛 💚 💙 🩵 💜 🤎 🖤 🩶 🤍 💋 💯 💢 🫯 💥 💫 💦 💨 🕳️ 💬 👁️‍🗨️ 🗨️ 🗯️ 💭 💤"),
    ("emoji_category_people", "👋 👋🏻 👋🏼 👋🏽 👋🏾 👋🏿 🤚 🤚🏻 🤚🏼 🤚🏽 🤚🏾 🤚🏿 🖐️ 🖐🏻 🖐🏼 🖐🏽 🖐🏾 🖐🏿 ✋ ✋🏻 ✋🏼 ✋🏽 ✋🏾 ✋🏿 🖖 🖖🏻 🖖🏼 🖖🏽 🖖🏾 🖖🏿 🫱 🫱🏻 🫱🏼 🫱🏽 🫱🏾 🫱🏿 🫲 🫲🏻 🫲🏼 🫲🏽 🫲🏾 🫲🏿 🫳 🫳🏻 🫳🏼 🫳🏽 🫳🏾 🫳🏿 🫴 🫴🏻 🫴🏼 🫴🏽 🫴🏾 🫴🏿 🫷 🫷🏻 🫷🏼 🫷🏽 🫷🏾 🫷🏿 🫸 🫸🏻 🫸🏼 🫸🏽 🫸🏾 🫸🏿 👌 👌🏻 👌🏼 👌🏽 👌🏾 👌🏿 🤌 🤌🏻 🤌🏼 🤌🏽 🤌🏾 🤌🏿 🤏 🤏🏻 🤏🏼 🤏🏽 🤏🏾 🤏🏿 ✌️ ✌🏻 ✌🏼 ✌🏽 ✌🏾 ✌🏿 🤞 🤞🏻 🤞🏼 🤞🏽 🤞🏾 🤞🏿 🫰 🫰🏻 🫰🏼 🫰🏽 🫰🏾 🫰🏿 🤟 🤟🏻 🤟🏼 🤟🏽 🤟🏾 🤟🏿 🤘 🤘🏻 🤘🏼 🤘🏽 🤘🏾 🤘🏿 🤙 🤙🏻 🤙🏼 🤙🏽 🤙🏾 🤙🏿 👈 👈🏻 👈🏼 👈🏽 👈🏾 👈🏿 👉 👉🏻 👉🏼 👉🏽 👉🏾 👉🏿 👆 👆🏻 👆🏼 👆🏽 👆🏾 👆🏿 🖕 🖕🏻 🖕🏼 🖕🏽 🖕🏾 🖕🏿 👇 👇🏻 👇🏼 👇🏽 👇🏾 👇🏿 ☝️ ☝🏻 ☝🏼 ☝🏽 ☝🏾 ☝🏿 🫵 🫵🏻 🫵🏼 🫵🏽 🫵🏾 🫵🏿 👍 👍🏻 👍🏼 👍🏽 👍🏾 👍🏿 👎 👎🏻 👎🏼 👎🏽 👎🏾 👎🏿 ✊ ✊🏻 ✊🏼 ✊🏽 ✊🏾 ✊🏿 👊 👊🏻 👊🏼 👊🏽 👊🏾 👊🏿 🤛 🤛🏻 🤛🏼 🤛🏽 🤛🏾 🤛🏿 🤜 🤜🏻 🤜🏼 🤜🏽 🤜🏾 🤜🏿 👏 👏🏻 👏🏼 👏🏽 👏🏾 👏🏿 🙌 🙌🏻 🙌🏼 🙌🏽 🙌🏾 🙌🏿 🫶 🫶🏻 🫶🏼 🫶🏽 🫶🏾 🫶🏿 👐 👐🏻 👐🏼 👐🏽 👐🏾 👐🏿 🤲 🤲🏻 🤲🏼 🤲🏽 🤲🏾 🤲🏿 🤝 🤝🏻 🤝🏼 🤝🏽 🤝🏾 🤝🏿 🫱🏻‍🫲🏼 🫱🏻‍🫲🏽 🫱🏻‍🫲🏾 🫱🏻‍🫲🏿 🫱🏼‍🫲🏻 🫱🏼‍🫲🏽 🫱🏼‍🫲🏾 🫱🏼‍🫲🏿 🫱🏽‍🫲🏻 🫱🏽‍🫲🏼 🫱🏽‍🫲🏾 🫱🏽‍🫲🏿 🫱🏾‍🫲🏻 🫱🏾‍🫲🏼 🫱🏾‍🫲🏽 🫱🏾‍🫲🏿 🫱🏿‍🫲🏻 🫱🏿‍🫲🏼 🫱🏿‍🫲🏽 🫱🏿‍🫲🏾 🙏 🙏🏻 🙏🏼 🙏🏽 🙏🏾 🙏🏿 ✍️ ✍🏻 ✍🏼 ✍🏽 ✍🏾 ✍🏿 💅 💅🏻 💅🏼 💅🏽 💅🏾 💅🏿 🤳 🤳🏻 🤳🏼 🤳🏽 🤳🏾 🤳🏿 💪 💪🏻 💪🏼 💪🏽 💪🏾 💪🏿 🦾 🦿 🦵 🦵🏻 🦵🏼 🦵🏽 🦵🏾 🦵🏿 🦶 🦶🏻 🦶🏼 🦶🏽 🦶🏾 🦶🏿 👂 👂🏻 👂🏼 👂🏽 👂🏾 👂🏿 🦻 🦻🏻 🦻🏼 🦻🏽 🦻🏾 🦻🏿 👃 👃🏻 👃🏼 👃🏽 👃🏾 👃🏿 🧠 🫀 🫁 🦷 🦴 👀 👁️ 👅 👄 🫦 👶 👶🏻 👶🏼 👶🏽 👶🏾 👶🏿 🧒 🧒🏻 🧒🏼 🧒🏽 🧒🏾 🧒🏿 👦 👦🏻 👦🏼 👦🏽 👦🏾 👦🏿 👧 👧🏻 👧🏼 👧🏽 👧🏾 👧🏿 🧑 🧑🏻 🧑🏼 🧑🏽 🧑🏾 🧑🏿 👱 👱🏻 👱🏼 👱🏽 👱🏾 👱🏿 👨 👨🏻 👨🏼 👨🏽 👨🏾 👨🏿 🧔 🧔🏻 🧔🏼 🧔🏽 🧔🏾 🧔🏿 🧔‍♂️ 🧔🏻‍♂️ 🧔🏼‍♂️ 🧔🏽‍♂️ 🧔🏾‍♂️ 🧔🏿‍♂️ 🧔‍♀️ 🧔🏻‍♀️ 🧔🏼‍♀️ 🧔🏽‍♀️ 🧔🏾‍♀️ 🧔🏿‍♀️ 👨‍🦰 👨🏻‍🦰 👨🏼‍🦰 👨🏽‍🦰 👨🏾‍🦰 👨🏿‍🦰 👨‍🦱 👨🏻‍🦱 👨🏼‍🦱 👨🏽‍🦱 👨🏾‍🦱 👨🏿‍🦱 👨‍🦳 👨🏻‍🦳 👨🏼‍🦳 👨🏽‍🦳 👨🏾‍🦳 👨🏿‍🦳 👨‍🦲 👨🏻‍🦲 👨🏼‍🦲 👨🏽‍🦲 👨🏾‍🦲 👨🏿‍🦲 👩 👩🏻 👩🏼 👩🏽 👩🏾 👩🏿 👩‍🦰 👩🏻‍🦰 👩🏼‍🦰 👩🏽‍🦰 👩🏾‍🦰 👩🏿‍🦰 🧑‍🦰 🧑🏻‍🦰 🧑🏼‍🦰 🧑🏽‍🦰 🧑🏾‍🦰 🧑🏿‍🦰 👩‍🦱 👩🏻‍🦱 👩🏼‍🦱 👩🏽‍🦱 👩🏾‍🦱 👩🏿‍🦱 🧑‍🦱 🧑🏻‍🦱 🧑🏼‍🦱 🧑🏽‍🦱 🧑🏾‍🦱 🧑🏿‍🦱 👩‍🦳 👩🏻‍🦳 👩🏼‍🦳 👩🏽‍🦳 👩🏾‍🦳 👩🏿‍🦳 🧑‍🦳 🧑🏻‍🦳 🧑🏼‍🦳 🧑🏽‍🦳 🧑🏾‍🦳 🧑🏿‍🦳 👩‍🦲 👩🏻‍🦲 👩🏼‍🦲 👩🏽‍🦲 👩🏾‍🦲 👩🏿‍🦲 🧑‍🦲 🧑🏻‍🦲 🧑🏼‍🦲 🧑🏽‍🦲 🧑🏾‍🦲 🧑🏿‍🦲 👱‍♀️ 👱🏻‍♀️ 👱🏼‍♀️ 👱🏽‍♀️ 👱🏾‍♀️ 👱🏿‍♀️ 👱‍♂️ 👱🏻‍♂️ 👱🏼‍♂️ 👱🏽‍♂️ 👱🏾‍♂️ 👱🏿‍♂️ 🧓 🧓🏻 🧓🏼 🧓🏽 🧓🏾 🧓🏿 👴 👴🏻 👴🏼 👴🏽 👴🏾 👴🏿 👵 👵🏻 👵🏼 👵🏽 👵🏾 👵🏿 🙍 🙍🏻 🙍🏼 🙍🏽 🙍🏾 🙍🏿 🙍‍♂️ 🙍🏻‍♂️ 🙍🏼‍♂️ 🙍🏽‍♂️ 🙍🏾‍♂️ 🙍🏿‍♂️ 🙍‍♀️ 🙍🏻‍♀️ 🙍🏼‍♀️ 🙍🏽‍♀️ 🙍🏾‍♀️ 🙍🏿‍♀️ 🙎 🙎🏻 🙎🏼 🙎🏽 🙎🏾 🙎🏿 🙎‍♂️ 🙎🏻‍♂️ 🙎🏼‍♂️ 🙎🏽‍♂️ 🙎🏾‍♂️ 🙎🏿‍♂️ 🙎‍♀️ 🙎🏻‍♀️ 🙎🏼‍♀️ 🙎🏽‍♀️ 🙎🏾‍♀️ 🙎🏿‍♀️ 🙅 🙅🏻 🙅🏼 🙅🏽 🙅🏾 🙅🏿 🙅‍♂️ 🙅🏻‍♂️ 🙅🏼‍♂️ 🙅🏽‍♂️ 🙅🏾‍♂️ 🙅🏿‍♂️ 🙅‍♀️ 🙅🏻‍♀️ 🙅🏼‍♀️ 🙅🏽‍♀️ 🙅🏾‍♀️ 🙅🏿‍♀️ 🙆 🙆🏻 🙆🏼 🙆🏽 🙆🏾 🙆🏿 🙆‍♂️ 🙆🏻‍♂️ 🙆🏼‍♂️ 🙆🏽‍♂️ 🙆🏾‍♂️ 🙆🏿‍♂️ 🙆‍♀️ 🙆🏻‍♀️ 🙆🏼‍♀️ 🙆🏽‍♀️ 🙆🏾‍♀️ 🙆🏿‍♀️ 💁 💁🏻 💁🏼 💁🏽 💁🏾 💁🏿 💁‍♂️ 💁🏻‍♂️ 💁🏼‍♂️ 💁🏽‍♂️ 💁🏾‍♂️ 💁🏿‍♂️ 💁‍♀️ 💁🏻‍♀️ 💁🏼‍♀️ 💁🏽‍♀️ 💁🏾‍♀️ 💁🏿‍♀️ 🙋 🙋🏻 🙋🏼 🙋🏽 🙋🏾 🙋🏿 🙋‍♂️ 🙋🏻‍♂️ 🙋🏼‍♂️ 🙋🏽‍♂️ 🙋🏾‍♂️ 🙋🏿‍♂️ 🙋‍♀️ 🙋🏻‍♀️ 🙋🏼‍♀️ 🙋🏽‍♀️ 🙋🏾‍♀️ 🙋🏿‍♀️ 🧏 🧏🏻 🧏🏼 🧏🏽 🧏🏾 🧏🏿 🧏‍♂️ 🧏🏻‍♂️ 🧏🏼‍♂️ 🧏🏽‍♂️ 🧏🏾‍♂️ 🧏🏿‍♂️ 🧏‍♀️ 🧏🏻‍♀️ 🧏🏼‍♀️ 🧏🏽‍♀️ 🧏🏾‍♀️ 🧏🏿‍♀️ 🙇 🙇🏻 🙇🏼 🙇🏽 🙇🏾 🙇🏿 🙇‍♂️ 🙇🏻‍♂️ 🙇🏼‍♂️ 🙇🏽‍♂️ 🙇🏾‍♂️ 🙇🏿‍♂️ 🙇‍♀️ 🙇🏻‍♀️ 🙇🏼‍♀️ 🙇🏽‍♀️ 🙇🏾‍♀️ 🙇🏿‍♀️ 🤦 🤦🏻 🤦🏼 🤦🏽 🤦🏾 🤦🏿 🤦‍♂️ 🤦🏻‍♂️ 🤦🏼‍♂️ 🤦🏽‍♂️ 🤦🏾‍♂️ 🤦🏿‍♂️ 🤦‍♀️ 🤦🏻‍♀️ 🤦🏼‍♀️ 🤦🏽‍♀️ 🤦🏾‍♀️ 🤦🏿‍♀️ 🤷 🤷🏻 🤷🏼 🤷🏽 🤷🏾 🤷🏿 🤷‍♂️ 🤷🏻‍♂️ 🤷🏼‍♂️ 🤷🏽‍♂️ 🤷🏾‍♂️ 🤷🏿‍♂️ 🤷‍♀️ 🤷🏻‍♀️ 🤷🏼‍♀️ 🤷🏽‍♀️ 🤷🏾‍♀️ 🤷🏿‍♀️ 🧑‍⚕️ 🧑🏻‍⚕️ 🧑🏼‍⚕️ 🧑🏽‍⚕️ 🧑🏾‍⚕️ 🧑🏿‍⚕️ 👨‍⚕️ 👨🏻‍⚕️ 👨🏼‍⚕️ 👨🏽‍⚕️ 👨🏾‍⚕️ 👨🏿‍⚕️ 👩‍⚕️ 👩🏻‍⚕️ 👩🏼‍⚕️ 👩🏽‍⚕️ 👩🏾‍⚕️ 👩🏿‍⚕️ 🧑‍🎓 🧑🏻‍🎓 🧑🏼‍🎓 🧑🏽‍🎓 🧑🏾‍🎓 🧑🏿‍🎓 👨‍🎓 👨🏻‍🎓 👨🏼‍🎓 👨🏽‍🎓 👨🏾‍🎓 👨🏿‍🎓 👩‍🎓 👩🏻‍🎓 👩🏼‍🎓 👩🏽‍🎓 👩🏾‍🎓 👩🏿‍🎓 🧑‍🏫 🧑🏻‍🏫 🧑🏼‍🏫 🧑🏽‍🏫 🧑🏾‍🏫 🧑🏿‍🏫 👨‍🏫 👨🏻‍🏫 👨🏼‍🏫 👨🏽‍🏫 👨🏾‍🏫 👨🏿‍🏫 👩‍🏫 👩🏻‍🏫 👩🏼‍🏫 👩🏽‍🏫 👩🏾‍🏫 👩🏿‍🏫 🧑‍⚖️ 🧑🏻‍⚖️ 🧑🏼‍⚖️ 🧑🏽‍⚖️ 🧑🏾‍⚖️ 🧑🏿‍⚖️ 👨‍⚖️ 👨🏻‍⚖️ 👨🏼‍⚖️ 👨🏽‍⚖️ 👨🏾‍⚖️ 👨🏿‍⚖️ 👩‍⚖️ 👩🏻‍⚖️ 👩🏼‍⚖️ 👩🏽‍⚖️ 👩🏾‍⚖️ 👩🏿‍⚖️ 🧑‍🌾 🧑🏻‍🌾 🧑🏼‍🌾 🧑🏽‍🌾 🧑🏾‍🌾 🧑🏿‍🌾 👨‍🌾 👨🏻‍🌾 👨🏼‍🌾 👨🏽‍🌾 👨🏾‍🌾 👨🏿‍🌾 👩‍🌾 👩🏻‍🌾 👩🏼‍🌾 👩🏽‍🌾 👩🏾‍🌾 👩🏿‍🌾 🧑‍🍳 🧑🏻‍🍳 🧑🏼‍🍳 🧑🏽‍🍳 🧑🏾‍🍳 🧑🏿‍🍳 👨‍🍳 👨🏻‍🍳 👨🏼‍🍳 👨🏽‍🍳 👨🏾‍🍳 👨🏿‍🍳 👩‍🍳 👩🏻‍🍳 👩🏼‍🍳 👩🏽‍🍳 👩🏾‍🍳 👩🏿‍🍳 🧑‍🔧 🧑🏻‍🔧 🧑🏼‍🔧 🧑🏽‍🔧 🧑🏾‍🔧 🧑🏿‍🔧 👨‍🔧 👨🏻‍🔧 👨🏼‍🔧 👨🏽‍🔧 👨🏾‍🔧 👨🏿‍🔧 👩‍🔧 👩🏻‍🔧 👩🏼‍🔧 👩🏽‍🔧 👩🏾‍🔧 👩🏿‍🔧 🧑‍🏭 🧑🏻‍🏭 🧑🏼‍🏭 🧑🏽‍🏭 🧑🏾‍🏭 🧑🏿‍🏭 👨‍🏭 👨🏻‍🏭 👨🏼‍🏭 👨🏽‍🏭 👨🏾‍🏭 👨🏿‍🏭 👩‍🏭 👩🏻‍🏭 👩🏼‍🏭 👩🏽‍🏭 👩🏾‍🏭 👩🏿‍🏭 🧑‍💼 🧑🏻‍💼 🧑🏼‍💼 🧑🏽‍💼 🧑🏾‍💼 🧑🏿‍💼 👨‍💼 👨🏻‍💼 👨🏼‍💼 👨🏽‍💼 👨🏾‍💼 👨🏿‍💼 👩‍💼 👩🏻‍💼 👩🏼‍💼 👩🏽‍💼 👩🏾‍💼 👩🏿‍💼 🧑‍🔬 🧑🏻‍🔬 🧑🏼‍🔬 🧑🏽‍🔬 🧑🏾‍🔬 🧑🏿‍🔬 👨‍🔬 👨🏻‍🔬 👨🏼‍🔬 👨🏽‍🔬 👨🏾‍🔬 👨🏿‍🔬 👩‍🔬 👩🏻‍🔬 👩🏼‍🔬 👩🏽‍🔬 👩🏾‍🔬 👩🏿‍🔬 🧑‍💻 🧑🏻‍💻 🧑🏼‍💻 🧑🏽‍💻 🧑🏾‍💻 🧑🏿‍💻 👨‍💻 👨🏻‍💻 👨🏼‍💻 👨🏽‍💻 👨🏾‍💻 👨🏿‍💻 👩‍💻 👩🏻‍💻 👩🏼‍💻 👩🏽‍💻 👩🏾‍💻 👩🏿‍💻 🧑‍🎤 🧑🏻‍🎤 🧑🏼‍🎤 🧑🏽‍🎤 🧑🏾‍🎤 🧑🏿‍🎤 👨‍🎤 👨🏻‍🎤 👨🏼‍🎤 👨🏽‍🎤 👨🏾‍🎤 👨🏿‍🎤 👩‍🎤 👩🏻‍🎤 👩🏼‍🎤 👩🏽‍🎤 👩🏾‍🎤 👩🏿‍🎤 🧑‍🎨 🧑🏻‍🎨 🧑🏼‍🎨 🧑🏽‍🎨 🧑🏾‍🎨 🧑🏿‍🎨 👨‍🎨 👨🏻‍🎨 👨🏼‍🎨 👨🏽‍🎨 👨🏾‍🎨 👨🏿‍🎨 👩‍🎨 👩🏻‍🎨 👩🏼‍🎨 👩🏽‍🎨 👩🏾‍🎨 👩🏿‍🎨 🧑‍✈️ 🧑🏻‍✈️ 🧑🏼‍✈️ 🧑🏽‍✈️ 🧑🏾‍✈️ 🧑🏿‍✈️ 👨‍✈️ 👨🏻‍✈️ 👨🏼‍✈️ 👨🏽‍✈️ 👨🏾‍✈️ 👨🏿‍✈️ 👩‍✈️ 👩🏻‍✈️ 👩🏼‍✈️ 👩🏽‍✈️ 👩🏾‍✈️ 👩🏿‍✈️ 🧑‍🚀 🧑🏻‍🚀 🧑🏼‍🚀 🧑🏽‍🚀 🧑🏾‍🚀 🧑🏿‍🚀 👨‍🚀 👨🏻‍🚀 👨🏼‍🚀 👨🏽‍🚀 👨🏾‍🚀 👨🏿‍🚀 👩‍🚀 👩🏻‍🚀 👩🏼‍🚀 👩🏽‍🚀 👩🏾‍🚀 👩🏿‍🚀 🧑‍🚒 🧑🏻‍🚒 🧑🏼‍🚒 🧑🏽‍🚒 🧑🏾‍🚒 🧑🏿‍🚒 👨‍🚒 👨🏻‍🚒 👨🏼‍🚒 👨🏽‍🚒 👨🏾‍🚒 👨🏿‍🚒 👩‍🚒 👩🏻‍🚒 👩🏼‍🚒 👩🏽‍🚒 👩🏾‍🚒 👩🏿‍🚒 👮 👮🏻 👮🏼 👮🏽 👮🏾 👮🏿 👮‍♂️ 👮🏻‍♂️ 👮🏼‍♂️ 👮🏽‍♂️ 👮🏾‍♂️ 👮🏿‍♂️ 👮‍♀️ 👮🏻‍♀️ 👮🏼‍♀️ 👮🏽‍♀️ 👮🏾‍♀️ 👮🏿‍♀️ 🕵️ 🕵🏻 🕵🏼 🕵🏽 🕵🏾 🕵🏿 🕵️‍♂️ 🕵🏻‍♂️ 🕵🏼‍♂️ 🕵🏽‍♂️ 🕵🏾‍♂️ 🕵🏿‍♂️ 🕵️‍♀️ 🕵🏻‍♀️ 🕵🏼‍♀️ 🕵🏽‍♀️ 🕵🏾‍♀️ 🕵🏿‍♀️ 💂 💂🏻 💂🏼 💂🏽 💂🏾 💂🏿 💂‍♂️ 💂🏻‍♂️ 💂🏼‍♂️ 💂🏽‍♂️ 💂🏾‍♂️ 💂🏿‍♂️ 💂‍♀️ 💂🏻‍♀️ 💂🏼‍♀️ 💂🏽‍♀️ 💂🏾‍♀️ 💂🏿‍♀️ 🥷 🥷🏻 🥷🏼 🥷🏽 🥷🏾 🥷🏿 👷 👷🏻 👷🏼 👷🏽 👷🏾 👷🏿 👷‍♂️ 👷🏻‍♂️ 👷🏼‍♂️ 👷🏽‍♂️ 👷🏾‍♂️ 👷🏿‍♂️ 👷‍♀️ 👷🏻‍♀️ 👷🏼‍♀️ 👷🏽‍♀️ 👷🏾‍♀️ 👷🏿‍♀️ 🫅 🫅🏻 🫅🏼 🫅🏽 🫅🏾 🫅🏿 🤴 🤴🏻 🤴🏼 🤴🏽 🤴🏾 🤴🏿 👸 👸🏻 👸🏼 👸🏽 👸🏾 👸🏿 👳 👳🏻 👳🏼 👳🏽 👳🏾 👳🏿 👳‍♂️ 👳🏻‍♂️ 👳🏼‍♂️ 👳🏽‍♂️ 👳🏾‍♂️ 👳🏿‍♂️ 👳‍♀️ 👳🏻‍♀️ 👳🏼‍♀️ 👳🏽‍♀️ 👳🏾‍♀️ 👳🏿‍♀️ 👲 👲🏻 👲🏼 👲🏽 👲🏾 👲🏿 🧕 🧕🏻 🧕🏼 🧕🏽 🧕🏾 🧕🏿 🤵 🤵🏻 🤵🏼 🤵🏽 🤵🏾 🤵🏿 🤵‍♂️ 🤵🏻‍♂️ 🤵🏼‍♂️ 🤵🏽‍♂️ 🤵🏾‍♂️ 🤵🏿‍♂️ 🤵‍♀️ 🤵🏻‍♀️ 🤵🏼‍♀️ 🤵🏽‍♀️ 🤵🏾‍♀️ 🤵🏿‍♀️ 👰 👰🏻 👰🏼 👰🏽 👰🏾 👰🏿 👰‍♂️ 👰🏻‍♂️ 👰🏼‍♂️ 👰🏽‍♂️ 👰🏾‍♂️ 👰🏿‍♂️ 👰‍♀️ 👰🏻‍♀️ 👰🏼‍♀️ 👰🏽‍♀️ 👰🏾‍♀️ 👰🏿‍♀️ 🤰 🤰🏻 🤰🏼 🤰🏽 🤰🏾 🤰🏿 🫃 🫃🏻 🫃🏼 🫃🏽 🫃🏾 🫃🏿 🫄 🫄🏻 🫄🏼 🫄🏽 🫄🏾 🫄🏿 🤱 🤱🏻 🤱🏼 🤱🏽 🤱🏾 🤱🏿 👩‍🍼 👩🏻‍🍼 👩🏼‍🍼 👩🏽‍🍼 👩🏾‍🍼 👩🏿‍🍼 👨‍🍼 👨🏻‍🍼 👨🏼‍🍼 👨🏽‍🍼 👨🏾‍🍼 👨🏿‍🍼 🧑‍🍼 🧑🏻‍🍼 🧑🏼‍🍼 🧑🏽‍🍼 🧑🏾‍🍼 🧑🏿‍🍼 👼 👼🏻 👼🏼 👼🏽 👼🏾 👼🏿 🎅 🎅🏻 🎅🏼 🎅🏽 🎅🏾 🎅🏿 🤶 🤶🏻 🤶🏼 🤶🏽 🤶🏾 🤶🏿 🧑‍🎄 🧑🏻‍🎄 🧑🏼‍🎄 🧑🏽‍🎄 🧑🏾‍🎄 🧑🏿‍🎄 🦸 🦸🏻 🦸🏼 🦸🏽 🦸🏾 🦸🏿 🦸‍♂️ 🦸🏻‍♂️ 🦸🏼‍♂️ 🦸🏽‍♂️ 🦸🏾‍♂️ 🦸🏿‍♂️ 🦸‍♀️ 🦸🏻‍♀️ 🦸🏼‍♀️ 🦸🏽‍♀️ 🦸🏾‍♀️ 🦸🏿‍♀️ 🦹 🦹🏻 🦹🏼 🦹🏽 🦹🏾 🦹🏿 🦹‍♂️ 🦹🏻‍♂️ 🦹🏼‍♂️ 🦹🏽‍♂️ 🦹🏾‍♂️ 🦹🏿‍♂️ 🦹‍♀️ 🦹🏻‍♀️ 🦹🏼‍♀️ 🦹🏽‍♀️ 🦹🏾‍♀️ 🦹🏿‍♀️ 🧙 🧙🏻 🧙🏼 🧙🏽 🧙🏾 🧙🏿 🧙‍♂️ 🧙🏻‍♂️ 🧙🏼‍♂️ 🧙🏽‍♂️ 🧙🏾‍♂️ 🧙🏿‍♂️ 🧙‍♀️ 🧙🏻‍♀️ 🧙🏼‍♀️ 🧙🏽‍♀️ 🧙🏾‍♀️ 🧙🏿‍♀️ 🧚 🧚🏻 🧚🏼 🧚🏽 🧚🏾 🧚🏿 🧚‍♂️ 🧚🏻‍♂️ 🧚🏼‍♂️ 🧚🏽‍♂️ 🧚🏾‍♂️ 🧚🏿‍♂️ 🧚‍♀️ 🧚🏻‍♀️ 🧚🏼‍♀️ 🧚🏽‍♀️ 🧚🏾‍♀️ 🧚🏿‍♀️ 🧛 🧛🏻 🧛🏼 🧛🏽 🧛🏾 🧛🏿 🧛‍♂️ 🧛🏻‍♂️ 🧛🏼‍♂️ 🧛🏽‍♂️ 🧛🏾‍♂️ 🧛🏿‍♂️ 🧛‍♀️ 🧛🏻‍♀️ 🧛🏼‍♀️ 🧛🏽‍♀️ 🧛🏾‍♀️ 🧛🏿‍♀️ 🧜 🧜🏻 🧜🏼 🧜🏽 🧜🏾 🧜🏿 🧜‍♂️ 🧜🏻‍♂️ 🧜🏼‍♂️ 🧜🏽‍♂️ 🧜🏾‍♂️ 🧜🏿‍♂️ 🧜‍♀️ 🧜🏻‍♀️ 🧜🏼‍♀️ 🧜🏽‍♀️ 🧜🏾‍♀️ 🧜🏿‍♀️ 🧝 🧝🏻 🧝🏼 🧝🏽 🧝🏾 🧝🏿 🧝‍♂️ 🧝🏻‍♂️ 🧝🏼‍♂️ 🧝🏽‍♂️ 🧝🏾‍♂️ 🧝🏿‍♂️ 🧝‍♀️ 🧝🏻‍♀️ 🧝🏼‍♀️ 🧝🏽‍♀️ 🧝🏾‍♀️ 🧝🏿‍♀️ 🧞 🧞‍♂️ 🧞‍♀️ 🧟 🧟‍♂️ 🧟‍♀️ 🧌 🫈 💆 💆🏻 💆🏼 💆🏽 💆🏾 💆🏿 💆‍♂️ 💆🏻‍♂️ 💆🏼‍♂️ 💆🏽‍♂️ 💆🏾‍♂️ 💆🏿‍♂️ 💆‍♀️ 💆🏻‍♀️ 💆🏼‍♀️ 💆🏽‍♀️ 💆🏾‍♀️ 💆🏿‍♀️ 💇 💇🏻 💇🏼 💇🏽 💇🏾 💇🏿 💇‍♂️ 💇🏻‍♂️ 💇🏼‍♂️ 💇🏽‍♂️ 💇🏾‍♂️ 💇🏿‍♂️ 💇‍♀️ 💇🏻‍♀️ 💇🏼‍♀️ 💇🏽‍♀️ 💇🏾‍♀️ 💇🏿‍♀️ 🚶 🚶🏻 🚶🏼 🚶🏽 🚶🏾 🚶🏿 🚶‍♂️ 🚶🏻‍♂️ 🚶🏼‍♂️ 🚶🏽‍♂️ 🚶🏾‍♂️ 🚶🏿‍♂️ 🚶‍♀️ 🚶🏻‍♀️ 🚶🏼‍♀️ 🚶🏽‍♀️ 🚶🏾‍♀️ 🚶🏿‍♀️ 🚶‍➡️ 🚶🏻‍➡️ 🚶🏼‍➡️ 🚶🏽‍➡️ 🚶🏾‍➡️ 🚶🏿‍➡️ 🚶‍♀️‍➡️ 🚶🏻‍♀️‍➡️ 🚶🏼‍♀️‍➡️ 🚶🏽‍♀️‍➡️ 🚶🏾‍♀️‍➡️ 🚶🏿‍♀️‍➡️ 🚶‍♂️‍➡️ 🚶🏻‍♂️‍➡️ 🚶🏼‍♂️‍➡️ 🚶🏽‍♂️‍➡️ 🚶🏾‍♂️‍➡️ 🚶🏿‍♂️‍➡️ 🧍 🧍🏻 🧍🏼 🧍🏽 🧍🏾 🧍🏿 🧍‍♂️ 🧍🏻‍♂️ 🧍🏼‍♂️ 🧍🏽‍♂️ 🧍🏾‍♂️ 🧍🏿‍♂️ 🧍‍♀️ 🧍🏻‍♀️ 🧍🏼‍♀️ 🧍🏽‍♀️ 🧍🏾‍♀️ 🧍🏿‍♀️ 🧎 🧎🏻 🧎🏼 🧎🏽 🧎🏾 🧎🏿 🧎‍♂️ 🧎🏻‍♂️ 🧎🏼‍♂️ 🧎🏽‍♂️ 🧎🏾‍♂️ 🧎🏿‍♂️ 🧎‍♀️ 🧎🏻‍♀️ 🧎🏼‍♀️ 🧎🏽‍♀️ 🧎🏾‍♀️ 🧎🏿‍♀️ 🧎‍➡️ 🧎🏻‍➡️ 🧎🏼‍➡️ 🧎🏽‍➡️ 🧎🏾‍➡️ 🧎🏿‍➡️ 🧎‍♀️‍➡️ 🧎🏻‍♀️‍➡️ 🧎🏼‍♀️‍➡️ 🧎🏽‍♀️‍➡️ 🧎🏾‍♀️‍➡️ 🧎🏿‍♀️‍➡️ 🧎‍♂️‍➡️ 🧎🏻‍♂️‍➡️ 🧎🏼‍♂️‍➡️ 🧎🏽‍♂️‍➡️ 🧎🏾‍♂️‍➡️ 🧎🏿‍♂️‍➡️ 🧑‍🦯 🧑🏻‍🦯 🧑🏼‍🦯 🧑🏽‍🦯 🧑🏾‍🦯 🧑🏿‍🦯 🧑‍🦯‍➡️ 🧑🏻‍🦯‍➡️ 🧑🏼‍🦯‍➡️ 🧑🏽‍🦯‍➡️ 🧑🏾‍🦯‍➡️ 🧑🏿‍🦯‍➡️ 👨‍🦯 👨🏻‍🦯 👨🏼‍🦯 👨🏽‍🦯 👨🏾‍🦯 👨🏿‍🦯 👨‍🦯‍➡️ 👨🏻‍🦯‍➡️ 👨🏼‍🦯‍➡️ 👨🏽‍🦯‍➡️ 👨🏾‍🦯‍➡️ 👨🏿‍🦯‍➡️ 👩‍🦯 👩🏻‍🦯 👩🏼‍🦯 👩🏽‍🦯 👩🏾‍🦯 👩🏿‍🦯 👩‍🦯‍➡️ 👩🏻‍🦯‍➡️ 👩🏼‍🦯‍➡️ 👩🏽‍🦯‍➡️ 👩🏾‍🦯‍➡️ 👩🏿‍🦯‍➡️ 🧑‍🦼 🧑🏻‍🦼 🧑🏼‍🦼 🧑🏽‍🦼 🧑🏾‍🦼 🧑🏿‍🦼 🧑‍🦼‍➡️ 🧑🏻‍🦼‍➡️ 🧑🏼‍🦼‍➡️ 🧑🏽‍🦼‍➡️ 🧑🏾‍🦼‍➡️ 🧑🏿‍🦼‍➡️ 👨‍🦼 👨🏻‍🦼 👨🏼‍🦼 👨🏽‍🦼 👨🏾‍🦼 👨🏿‍🦼 👨‍🦼‍➡️ 👨🏻‍🦼‍➡️ 👨🏼‍🦼‍➡️ 👨🏽‍🦼‍➡️ 👨🏾‍🦼‍➡️ 👨🏿‍🦼‍➡️ 👩‍🦼 👩🏻‍🦼 👩🏼‍🦼 👩🏽‍🦼 👩🏾‍🦼 👩🏿‍🦼 👩‍🦼‍➡️ 👩🏻‍🦼‍➡️ 👩🏼‍🦼‍➡️ 👩🏽‍🦼‍➡️ 👩🏾‍🦼‍➡️ 👩🏿‍🦼‍➡️ 🧑‍🦽 🧑🏻‍🦽 🧑🏼‍🦽 🧑🏽‍🦽 🧑🏾‍🦽 🧑🏿‍🦽 🧑‍🦽‍➡️ 🧑🏻‍🦽‍➡️ 🧑🏼‍🦽‍➡️ 🧑🏽‍🦽‍➡️ 🧑🏾‍🦽‍➡️ 🧑🏿‍🦽‍➡️ 👨‍🦽 👨🏻‍🦽 👨🏼‍🦽 👨🏽‍🦽 👨🏾‍🦽 👨🏿‍🦽 👨‍🦽‍➡️ 👨🏻‍🦽‍➡️ 👨🏼‍🦽‍➡️ 👨🏽‍🦽‍➡️ 👨🏾‍🦽‍➡️ 👨🏿‍🦽‍➡️ 👩‍🦽 👩🏻‍🦽 👩🏼‍🦽 👩🏽‍🦽 👩🏾‍🦽 👩🏿‍🦽 👩‍🦽‍➡️ 👩🏻‍🦽‍➡️ 👩🏼‍🦽‍➡️ 👩🏽‍🦽‍➡️ 👩🏾‍🦽‍➡️ 👩🏿‍🦽‍➡️ 🏃 🏃🏻 🏃🏼 🏃🏽 🏃🏾 🏃🏿 🏃‍♂️ 🏃🏻‍♂️ 🏃🏼‍♂️ 🏃🏽‍♂️ 🏃🏾‍♂️ 🏃🏿‍♂️ 🏃‍♀️ 🏃🏻‍♀️ 🏃🏼‍♀️ 🏃🏽‍♀️ 🏃🏾‍♀️ 🏃🏿‍♀️ 🏃‍➡️ 🏃🏻‍➡️ 🏃🏼‍➡️ 🏃🏽‍➡️ 🏃🏾‍➡️ 🏃🏿‍➡️ 🏃‍♀️‍➡️ 🏃🏻‍♀️‍➡️ 🏃🏼‍♀️‍➡️ 🏃🏽‍♀️‍➡️ 🏃🏾‍♀️‍➡️ 🏃🏿‍♀️‍➡️ 🏃‍♂️‍➡️ 🏃🏻‍♂️‍➡️ 🏃🏼‍♂️‍➡️ 🏃🏽‍♂️‍➡️ 🏃🏾‍♂️‍➡️ 🏃🏿‍♂️‍➡️ 🧑‍🩰 🧑🏻‍🩰 🧑🏼‍🩰 🧑🏽‍🩰 🧑🏾‍🩰 🧑🏿‍🩰 💃 💃🏻 💃🏼 💃🏽 💃🏾 💃🏿 🕺 🕺🏻 🕺🏼 🕺🏽 🕺🏾 🕺🏿 🕴️ 🕴🏻 🕴🏼 🕴🏽 🕴🏾 🕴🏿 👯 👯🏻 👯🏼 👯🏽 👯🏾 👯🏿 👯‍♂️ 👯🏻‍♂️ 👯🏼‍♂️ 👯🏽‍♂️ 👯🏾‍♂️ 👯🏿‍♂️ 👯‍♀️ 👯🏻‍♀️ 👯🏼‍♀️ 👯🏽‍♀️ 👯🏾‍♀️ 👯🏿‍♀️ 🧑🏻‍🐰‍🧑🏼 🧑🏻‍🐰‍🧑🏽 🧑🏻‍🐰‍🧑🏾 🧑🏻‍🐰‍🧑🏿 🧑🏼‍🐰‍🧑🏻 🧑🏼‍🐰‍🧑🏽 🧑🏼‍🐰‍🧑🏾 🧑🏼‍🐰‍🧑🏿 🧑🏽‍🐰‍🧑🏻 🧑🏽‍🐰‍🧑🏼 🧑🏽‍🐰‍🧑🏾 🧑🏽‍🐰‍🧑🏿 🧑🏾‍🐰‍🧑🏻 🧑🏾‍🐰‍🧑🏼 🧑🏾‍🐰‍🧑🏽 🧑🏾‍🐰‍🧑🏿 🧑🏿‍🐰‍🧑🏻 🧑🏿‍🐰‍🧑🏼 🧑🏿‍🐰‍🧑🏽 🧑🏿‍🐰‍🧑🏾 👨🏻‍🐰‍👨🏼 👨🏻‍🐰‍👨🏽 👨🏻‍🐰‍👨🏾 👨🏻‍🐰‍👨🏿 👨🏼‍🐰‍👨🏻 👨🏼‍🐰‍👨🏽 👨🏼‍🐰‍👨🏾 👨🏼‍🐰‍👨🏿 👨🏽‍🐰‍👨🏻 👨🏽‍🐰‍👨🏼 👨🏽‍🐰‍👨🏾 👨🏽‍🐰‍👨🏿 👨🏾‍🐰‍👨🏻 👨🏾‍🐰‍👨🏼 👨🏾‍🐰‍👨🏽 👨🏾‍🐰‍👨🏿 👨🏿‍🐰‍👨🏻 👨🏿‍🐰‍👨🏼 👨🏿‍🐰‍👨🏽 👨🏿‍🐰‍👨🏾 👩🏻‍🐰‍👩🏼 👩🏻‍🐰‍👩🏽 👩🏻‍🐰‍👩🏾 👩🏻‍🐰‍👩🏿 👩🏼‍🐰‍👩🏻 👩🏼‍🐰‍👩🏽 👩🏼‍🐰‍👩🏾 👩🏼‍🐰‍👩🏿 👩🏽‍🐰‍👩🏻 👩🏽‍🐰‍👩🏼 👩🏽‍🐰‍👩🏾 👩🏽‍🐰‍👩🏿 👩🏾‍🐰‍👩🏻 👩🏾‍🐰‍👩🏼 👩🏾‍🐰‍👩🏽 👩🏾‍🐰‍👩🏿 👩🏿‍🐰‍👩🏻 👩🏿‍🐰‍👩🏼 👩🏿‍🐰‍👩🏽 👩🏿‍🐰‍👩🏾 🧖 🧖🏻 🧖🏼 🧖🏽 🧖🏾 🧖🏿 🧖‍♂️ 🧖🏻‍♂️ 🧖🏼‍♂️ 🧖🏽‍♂️ 🧖🏾‍♂️ 🧖🏿‍♂️ 🧖‍♀️ 🧖🏻‍♀️ 🧖🏼‍♀️ 🧖🏽‍♀️ 🧖🏾‍♀️ 🧖🏿‍♀️ 🧗 🧗🏻 🧗🏼 🧗🏽 🧗🏾 🧗🏿 🧗‍♂️ 🧗🏻‍♂️ 🧗🏼‍♂️ 🧗🏽‍♂️ 🧗🏾‍♂️ 🧗🏿‍♂️ 🧗‍♀️ 🧗🏻‍♀️ 🧗🏼‍♀️ 🧗🏽‍♀️ 🧗🏾‍♀️ 🧗🏿‍♀️ 🤺 🏇 🏇🏻 🏇🏼 🏇🏽 🏇🏾 🏇🏿 ⛷️ 🏂 🏂🏻 🏂🏼 🏂🏽 🏂🏾 🏂🏿 🏌️ 🏌🏻 🏌🏼 🏌🏽 🏌🏾 🏌🏿 🏌️‍♂️ 🏌🏻‍♂️ 🏌🏼‍♂️ 🏌🏽‍♂️ 🏌🏾‍♂️ 🏌🏿‍♂️ 🏌️‍♀️ 🏌🏻‍♀️ 🏌🏼‍♀️ 🏌🏽‍♀️ 🏌🏾‍♀️ 🏌🏿‍♀️ 🏄 🏄🏻 🏄🏼 🏄🏽 🏄🏾 🏄🏿 🏄‍♂️ 🏄🏻‍♂️ 🏄🏼‍♂️ 🏄🏽‍♂️ 🏄🏾‍♂️ 🏄🏿‍♂️ 🏄‍♀️ 🏄🏻‍♀️ 🏄🏼‍♀️ 🏄🏽‍♀️ 🏄🏾‍♀️ 🏄🏿‍♀️ 🚣 🚣🏻 🚣🏼 🚣🏽 🚣🏾 🚣🏿 🚣‍♂️ 🚣🏻‍♂️ 🚣🏼‍♂️ 🚣🏽‍♂️ 🚣🏾‍♂️ 🚣🏿‍♂️ 🚣‍♀️ 🚣🏻‍♀️ 🚣🏼‍♀️ 🚣🏽‍♀️ 🚣🏾‍♀️ 🚣🏿‍♀️ 🏊 🏊🏻 🏊🏼 🏊🏽 🏊🏾 🏊🏿 🏊‍♂️ 🏊🏻‍♂️ 🏊🏼‍♂️ 🏊🏽‍♂️ 🏊🏾‍♂️ 🏊🏿‍♂️ 🏊‍♀️ 🏊🏻‍♀️ 🏊🏼‍♀️ 🏊🏽‍♀️ 🏊🏾‍♀️ 🏊🏿‍♀️ ⛹️ ⛹🏻 ⛹🏼 ⛹🏽 ⛹🏾 ⛹🏿 ⛹️‍♂️ ⛹🏻‍♂️ ⛹🏼‍♂️ ⛹🏽‍♂️ ⛹🏾‍♂️ ⛹🏿‍♂️ ⛹️‍♀️ ⛹🏻‍♀️ ⛹🏼‍♀️ ⛹🏽‍♀️ ⛹🏾‍♀️ ⛹🏿‍♀️ 🏋️ 🏋🏻 🏋🏼 🏋🏽 🏋🏾 🏋🏿 🏋️‍♂️ 🏋🏻‍♂️ 🏋🏼‍♂️ 🏋🏽‍♂️ 🏋🏾‍♂️ 🏋🏿‍♂️ 🏋️‍♀️ 🏋🏻‍♀️ 🏋🏼‍♀️ 🏋🏽‍♀️ 🏋🏾‍♀️ 🏋🏿‍♀️ 🚴 🚴🏻 🚴🏼 🚴🏽 🚴🏾 🚴🏿 🚴‍♂️ 🚴🏻‍♂️ 🚴🏼‍♂️ 🚴🏽‍♂️ 🚴🏾‍♂️ 🚴🏿‍♂️ 🚴‍♀️ 🚴🏻‍♀️ 🚴🏼‍♀️ 🚴🏽‍♀️ 🚴🏾‍♀️ 🚴🏿‍♀️ 🚵 🚵🏻 🚵🏼 🚵🏽 🚵🏾 🚵🏿 🚵‍♂️ 🚵🏻‍♂️ 🚵🏼‍♂️ 🚵🏽‍♂️ 🚵🏾‍♂️ 🚵🏿‍♂️ 🚵‍♀️ 🚵🏻‍♀️ 🚵🏼‍♀️ 🚵🏽‍♀️ 🚵🏾‍♀️ 🚵🏿‍♀️ 🤸 🤸🏻 🤸🏼 🤸🏽 🤸🏾 🤸🏿 🤸‍♂️ 🤸🏻‍♂️ 🤸🏼‍♂️ 🤸🏽‍♂️ 🤸🏾‍♂️ 🤸🏿‍♂️ 🤸‍♀️ 🤸🏻‍♀️ 🤸🏼‍♀️ 🤸🏽‍♀️ 🤸🏾‍♀️ 🤸🏿‍♀️ 🤼 🤼🏻 🤼🏼 🤼🏽 🤼🏾 🤼🏿 🤼‍♂️ 🤼🏻‍♂️ 🤼🏼‍♂️ 🤼🏽‍♂️ 🤼🏾‍♂️ 🤼🏿‍♂️ 🤼‍♀️ 🤼🏻‍♀️ 🤼🏼‍♀️ 🤼🏽‍♀️ 🤼🏾‍♀️ 🤼🏿‍♀️ 🧑🏻‍🫯‍🧑🏼 🧑🏻‍🫯‍🧑🏽 🧑🏻‍🫯‍🧑🏾 🧑🏻‍🫯‍🧑🏿 🧑🏼‍🫯‍🧑🏻 🧑🏼‍🫯‍🧑🏽 🧑🏼‍🫯‍🧑🏾 🧑🏼‍🫯‍🧑🏿 🧑🏽‍🫯‍🧑🏻 🧑🏽‍🫯‍🧑🏼 🧑🏽‍🫯‍🧑🏾 🧑🏽‍🫯‍🧑🏿 🧑🏾‍🫯‍🧑🏻 🧑🏾‍🫯‍🧑🏼 🧑🏾‍🫯‍🧑🏽 🧑🏾‍🫯‍🧑🏿 🧑🏿‍🫯‍🧑🏻 🧑🏿‍🫯‍🧑🏼 🧑🏿‍🫯‍🧑🏽 🧑🏿‍🫯‍🧑🏾 👨🏻‍🫯‍👨🏼 👨🏻‍🫯‍👨🏽 👨🏻‍🫯‍👨🏾 👨🏻‍🫯‍👨🏿 👨🏼‍🫯‍👨🏻 👨🏼‍🫯‍👨🏽 👨🏼‍🫯‍👨🏾 👨🏼‍🫯‍👨🏿 👨🏽‍🫯‍👨🏻 👨🏽‍🫯‍👨🏼 👨🏽‍🫯‍👨🏾 👨🏽‍🫯‍👨🏿 👨🏾‍🫯‍👨🏻 👨🏾‍🫯‍👨🏼 👨🏾‍🫯‍👨🏽 👨🏾‍🫯‍👨🏿 👨🏿‍🫯‍👨🏻 👨🏿‍🫯‍👨🏼 👨🏿‍🫯‍👨🏽 👨🏿‍🫯‍👨🏾 👩🏻‍🫯‍👩🏼 👩🏻‍🫯‍👩🏽 👩🏻‍🫯‍👩🏾 👩🏻‍🫯‍👩🏿 👩🏼‍🫯‍👩🏻 👩🏼‍🫯‍👩🏽 👩🏼‍🫯‍👩🏾 👩🏼‍🫯‍👩🏿 👩🏽‍🫯‍👩🏻 👩🏽‍🫯‍👩🏼 👩🏽‍🫯‍👩🏾 👩🏽‍🫯‍👩🏿 👩🏾‍🫯‍👩🏻 👩🏾‍🫯‍👩🏼 👩🏾‍🫯‍👩🏽 👩🏾‍🫯‍👩🏿 👩🏿‍🫯‍👩🏻 👩🏿‍🫯‍👩🏼 👩🏿‍🫯‍👩🏽 👩🏿‍🫯‍👩🏾 🤽 🤽🏻 🤽🏼 🤽🏽 🤽🏾 🤽🏿 🤽‍♂️ 🤽🏻‍♂️ 🤽🏼‍♂️ 🤽🏽‍♂️ 🤽🏾‍♂️ 🤽🏿‍♂️ 🤽‍♀️ 🤽🏻‍♀️ 🤽🏼‍♀️ 🤽🏽‍♀️ 🤽🏾‍♀️ 🤽🏿‍♀️ 🤾 🤾🏻 🤾🏼 🤾🏽 🤾🏾 🤾🏿 🤾‍♂️ 🤾🏻‍♂️ 🤾🏼‍♂️ 🤾🏽‍♂️ 🤾🏾‍♂️ 🤾🏿‍♂️ 🤾‍♀️ 🤾🏻‍♀️ 🤾🏼‍♀️ 🤾🏽‍♀️ 🤾🏾‍♀️ 🤾🏿‍♀️ 🤹 🤹🏻 🤹🏼 🤹🏽 🤹🏾 🤹🏿 🤹‍♂️ 🤹🏻‍♂️ 🤹🏼‍♂️ 🤹🏽‍♂️ 🤹🏾‍♂️ 🤹🏿‍♂️ 🤹‍♀️ 🤹🏻‍♀️ 🤹🏼‍♀️ 🤹🏽‍♀️ 🤹🏾‍♀️ 🤹🏿‍♀️ 🧘 🧘🏻 🧘🏼 🧘🏽 🧘🏾 🧘🏿 🧘‍♂️ 🧘🏻‍♂️ 🧘🏼‍♂️ 🧘🏽‍♂️ 🧘🏾‍♂️ 🧘🏿‍♂️ 🧘‍♀️ 🧘🏻‍♀️ 🧘🏼‍♀️ 🧘🏽‍♀️ 🧘🏾‍♀️ 🧘🏿‍♀️ 🛀 🛀🏻 🛀🏼 🛀🏽 🛀🏾 🛀🏿 🛌 🛌🏻 🛌🏼 🛌🏽 🛌🏾 🛌🏿 🧑‍🤝‍🧑 🧑🏻‍🤝‍🧑🏻 🧑🏻‍🤝‍🧑🏼 🧑🏻‍🤝‍🧑🏽 🧑🏻‍🤝‍🧑🏾 🧑🏻‍🤝‍🧑🏿 🧑🏼‍🤝‍🧑🏻 🧑🏼‍🤝‍🧑🏼 🧑🏼‍🤝‍🧑🏽 🧑🏼‍🤝‍🧑🏾 🧑🏼‍🤝‍🧑🏿 🧑🏽‍🤝‍🧑🏻 🧑🏽‍🤝‍🧑🏼 🧑🏽‍🤝‍🧑🏽 🧑🏽‍🤝‍🧑🏾 🧑🏽‍🤝‍🧑🏿 🧑🏾‍🤝‍🧑🏻 🧑🏾‍🤝‍🧑🏼 🧑🏾‍🤝‍🧑🏽 🧑🏾‍🤝‍🧑🏾 🧑🏾‍🤝‍🧑🏿 🧑🏿‍🤝‍🧑🏻 🧑🏿‍🤝‍🧑🏼 🧑🏿‍🤝‍🧑🏽 🧑🏿‍🤝‍🧑🏾 🧑🏿‍🤝‍🧑🏿 👭 👭🏻 👩🏻‍🤝‍👩🏼 👩🏻‍🤝‍👩🏽 👩🏻‍🤝‍👩🏾 👩🏻‍🤝‍👩🏿 👩🏼‍🤝‍👩🏻 👭🏼 👩🏼‍🤝‍👩🏽 👩🏼‍🤝‍👩🏾 👩🏼‍🤝‍👩🏿 👩🏽‍🤝‍👩🏻 👩🏽‍🤝‍👩🏼 👭🏽 👩🏽‍🤝‍👩🏾 👩🏽‍🤝‍👩🏿 👩🏾‍🤝‍👩🏻 👩🏾‍🤝‍👩🏼 👩🏾‍🤝‍👩🏽 👭🏾 👩🏾‍🤝‍👩🏿 👩🏿‍🤝‍👩🏻 👩🏿‍🤝‍👩🏼 👩🏿‍🤝‍👩🏽 👩🏿‍🤝‍👩🏾 👭🏿 👫 👫🏻 👩🏻‍🤝‍👨🏼 👩🏻‍🤝‍👨🏽 👩🏻‍🤝‍👨🏾 👩🏻‍🤝‍👨🏿 👩🏼‍🤝‍👨🏻 👫🏼 👩🏼‍🤝‍👨🏽 👩🏼‍🤝‍👨🏾 👩🏼‍🤝‍👨🏿 👩🏽‍🤝‍👨🏻 👩🏽‍🤝‍👨🏼 👫🏽 👩🏽‍🤝‍👨🏾 👩🏽‍🤝‍👨🏿 👩🏾‍🤝‍👨🏻 👩🏾‍🤝‍👨🏼 👩🏾‍🤝‍👨🏽 👫🏾 👩🏾‍🤝‍👨🏿 👩🏿‍🤝‍👨🏻 👩🏿‍🤝‍👨🏼 👩🏿‍🤝‍👨🏽 👩🏿‍🤝‍👨🏾 👫🏿 👬 👬🏻 👨🏻‍🤝‍👨🏼 👨🏻‍🤝‍👨🏽 👨🏻‍🤝‍👨🏾 👨🏻‍🤝‍👨🏿 👨🏼‍🤝‍👨🏻 👬🏼 👨🏼‍🤝‍👨🏽 👨🏼‍🤝‍👨🏾 👨🏼‍🤝‍👨🏿 👨🏽‍🤝‍👨🏻 👨🏽‍🤝‍👨🏼 👬🏽 👨🏽‍🤝‍👨🏾 👨🏽‍🤝‍👨🏿 👨🏾‍🤝‍👨🏻 👨🏾‍🤝‍👨🏼 👨🏾‍🤝‍👨🏽 👬🏾 👨🏾‍🤝‍👨🏿 👨🏿‍🤝‍👨🏻 👨🏿‍🤝‍👨🏼 👨🏿‍🤝‍👨🏽 👨🏿‍🤝‍👨🏾 👬🏿 💏 💏🏻 💏🏼 💏🏽 💏🏾 💏🏿 🧑🏻‍❤️‍💋‍🧑🏼 🧑🏻‍❤️‍💋‍🧑🏽 🧑🏻‍❤️‍💋‍🧑🏾 🧑🏻‍❤️‍💋‍🧑🏿 🧑🏼‍❤️‍💋‍🧑🏻 🧑🏼‍❤️‍💋‍🧑🏽 🧑🏼‍❤️‍💋‍🧑🏾 🧑🏼‍❤️‍💋‍🧑🏿 🧑🏽‍❤️‍💋‍🧑🏻 🧑🏽‍❤️‍💋‍🧑🏼 🧑🏽‍❤️‍💋‍🧑🏾 🧑🏽‍❤️‍💋‍🧑🏿 🧑🏾‍❤️‍💋‍🧑🏻 🧑🏾‍❤️‍💋‍🧑🏼 🧑🏾‍❤️‍💋‍🧑🏽 🧑🏾‍❤️‍💋‍🧑🏿 🧑🏿‍❤️‍💋‍🧑🏻 🧑🏿‍❤️‍💋‍🧑🏼 🧑🏿‍❤️‍💋‍🧑🏽 🧑🏿‍❤️‍💋‍🧑🏾 👩‍❤️‍💋‍👨 👩🏻‍❤️‍💋‍👨🏻 👩🏻‍❤️‍💋‍👨🏼 👩🏻‍❤️‍💋‍👨🏽 👩🏻‍❤️‍💋‍👨🏾 👩🏻‍❤️‍💋‍👨🏿 👩🏼‍❤️‍💋‍👨🏻 👩🏼‍❤️‍💋‍👨🏼 👩🏼‍❤️‍💋‍👨🏽 👩🏼‍❤️‍💋‍👨🏾 👩🏼‍❤️‍💋‍👨🏿 👩🏽‍❤️‍💋‍👨🏻 👩🏽‍❤️‍💋‍👨🏼 👩🏽‍❤️‍💋‍👨🏽 👩🏽‍❤️‍💋‍👨🏾 👩🏽‍❤️‍💋‍👨🏿 👩🏾‍❤️‍💋‍👨🏻 👩🏾‍❤️‍💋‍👨🏼 👩🏾‍❤️‍💋‍👨🏽 👩🏾‍❤️‍💋‍👨🏾 👩🏾‍❤️‍💋‍👨🏿 👩🏿‍❤️‍💋‍👨🏻 👩🏿‍❤️‍💋‍👨🏼 👩🏿‍❤️‍💋‍👨🏽 👩🏿‍❤️‍💋‍👨🏾 👩🏿‍❤️‍💋‍👨🏿 👨‍❤️‍💋‍👨 👨🏻‍❤️‍💋‍👨🏻 👨🏻‍❤️‍💋‍👨🏼 👨🏻‍❤️‍💋‍👨🏽 👨🏻‍❤️‍💋‍👨🏾 👨🏻‍❤️‍💋‍👨🏿 👨🏼‍❤️‍💋‍👨🏻 👨🏼‍❤️‍💋‍👨🏼 👨🏼‍❤️‍💋‍👨🏽 👨🏼‍❤️‍💋‍👨🏾 👨🏼‍❤️‍💋‍👨🏿 👨🏽‍❤️‍💋‍👨🏻 👨🏽‍❤️‍💋‍👨🏼 👨🏽‍❤️‍💋‍👨🏽 👨🏽‍❤️‍💋‍👨🏾 👨🏽‍❤️‍💋‍👨🏿 👨🏾‍❤️‍💋‍👨🏻 👨🏾‍❤️‍💋‍👨🏼 👨🏾‍❤️‍💋‍👨🏽 👨🏾‍❤️‍💋‍👨🏾 👨🏾‍❤️‍💋‍👨🏿 👨🏿‍❤️‍💋‍👨🏻 👨🏿‍❤️‍💋‍👨🏼 👨🏿‍❤️‍💋‍👨🏽 👨🏿‍❤️‍💋‍👨🏾 👨🏿‍❤️‍💋‍👨🏿 👩‍❤️‍💋‍👩 👩🏻‍❤️‍💋‍👩🏻 👩🏻‍❤️‍💋‍👩🏼 👩🏻‍❤️‍💋‍👩🏽 👩🏻‍❤️‍💋‍👩🏾 👩🏻‍❤️‍💋‍👩🏿 👩🏼‍❤️‍💋‍👩🏻 👩🏼‍❤️‍💋‍👩🏼 👩🏼‍❤️‍💋‍👩🏽 👩🏼‍❤️‍💋‍👩🏾 👩🏼‍❤️‍💋‍👩🏿 👩🏽‍❤️‍💋‍👩🏻 👩🏽‍❤️‍💋‍👩🏼 👩🏽‍❤️‍💋‍👩🏽 👩🏽‍❤️‍💋‍👩🏾 👩🏽‍❤️‍💋‍👩🏿 👩🏾‍❤️‍💋‍👩🏻 👩🏾‍❤️‍💋‍👩🏼 👩🏾‍❤️‍💋‍👩🏽 👩🏾‍❤️‍💋‍👩🏾 👩🏾‍❤️‍💋‍👩🏿 👩🏿‍❤️‍💋‍👩🏻 👩🏿‍❤️‍💋‍👩🏼 👩🏿‍❤️‍💋‍👩🏽 👩🏿‍❤️‍💋‍👩🏾 👩🏿‍❤️‍💋‍👩🏿 💑 💑🏻 💑🏼 💑🏽 💑🏾 💑🏿 🧑🏻‍❤️‍🧑🏼 🧑🏻‍❤️‍🧑🏽 🧑🏻‍❤️‍🧑🏾 🧑🏻‍❤️‍🧑🏿 🧑🏼‍❤️‍🧑🏻 🧑🏼‍❤️‍🧑🏽 🧑🏼‍❤️‍🧑🏾 🧑🏼‍❤️‍🧑🏿 🧑🏽‍❤️‍🧑🏻 🧑🏽‍❤️‍🧑🏼 🧑🏽‍❤️‍🧑🏾 🧑🏽‍❤️‍🧑🏿 🧑🏾‍❤️‍🧑🏻 🧑🏾‍❤️‍🧑🏼 🧑🏾‍❤️‍🧑🏽 🧑🏾‍❤️‍🧑🏿 🧑🏿‍❤️‍🧑🏻 🧑🏿‍❤️‍🧑🏼 🧑🏿‍❤️‍🧑🏽 🧑🏿‍❤️‍🧑🏾 👩‍❤️‍👨 👩🏻‍❤️‍👨🏻 👩🏻‍❤️‍👨🏼 👩🏻‍❤️‍👨🏽 👩🏻‍❤️‍👨🏾 👩🏻‍❤️‍👨🏿 👩🏼‍❤️‍👨🏻 👩🏼‍❤️‍👨🏼 👩🏼‍❤️‍👨🏽 👩🏼‍❤️‍👨🏾 👩🏼‍❤️‍👨🏿 👩🏽‍❤️‍👨🏻 👩🏽‍❤️‍👨🏼 👩🏽‍❤️‍👨🏽 👩🏽‍❤️‍👨🏾 👩🏽‍❤️‍👨🏿 👩🏾‍❤️‍👨🏻 👩🏾‍❤️‍👨🏼 👩🏾‍❤️‍👨🏽 👩🏾‍❤️‍👨🏾 👩🏾‍❤️‍👨🏿 👩🏿‍❤️‍👨🏻 👩🏿‍❤️‍👨🏼 👩🏿‍❤️‍👨🏽 👩🏿‍❤️‍👨🏾 👩🏿‍❤️‍👨🏿 👨‍❤️‍👨 👨🏻‍❤️‍👨🏻 👨🏻‍❤️‍👨🏼 👨🏻‍❤️‍👨🏽 👨🏻‍❤️‍👨🏾 👨🏻‍❤️‍👨🏿 👨🏼‍❤️‍👨🏻 👨🏼‍❤️‍👨🏼 👨🏼‍❤️‍👨🏽 👨🏼‍❤️‍👨🏾 👨🏼‍❤️‍👨🏿 👨🏽‍❤️‍👨🏻 👨🏽‍❤️‍👨🏼 👨🏽‍❤️‍👨🏽 👨🏽‍❤️‍👨🏾 👨🏽‍❤️‍👨🏿 👨🏾‍❤️‍👨🏻 👨🏾‍❤️‍👨🏼 👨🏾‍❤️‍👨🏽 👨🏾‍❤️‍👨🏾 👨🏾‍❤️‍👨🏿 👨🏿‍❤️‍👨🏻 👨🏿‍❤️‍👨🏼 👨🏿‍❤️‍👨🏽 👨🏿‍❤️‍👨🏾 👨🏿‍❤️‍👨🏿 👩‍❤️‍👩 👩🏻‍❤️‍👩🏻 👩🏻‍❤️‍👩🏼 👩🏻‍❤️‍👩🏽 👩🏻‍❤️‍👩🏾 👩🏻‍❤️‍👩🏿 👩🏼‍❤️‍👩🏻 👩🏼‍❤️‍👩🏼 👩🏼‍❤️‍👩🏽 👩🏼‍❤️‍👩🏾 👩🏼‍❤️‍👩🏿 👩🏽‍❤️‍👩🏻 👩🏽‍❤️‍👩🏼 👩🏽‍❤️‍👩🏽 👩🏽‍❤️‍👩🏾 👩🏽‍❤️‍👩🏿 👩🏾‍❤️‍👩🏻 👩🏾‍❤️‍👩🏼 👩🏾‍❤️‍👩🏽 👩🏾‍❤️‍👩🏾 👩🏾‍❤️‍👩🏿 👩🏿‍❤️‍👩🏻 👩🏿‍❤️‍👩🏼 👩🏿‍❤️‍👩🏽 👩🏿‍❤️‍👩🏾 👩🏿‍❤️‍👩🏿 👨‍👩‍👦 👨‍👩‍👧 👨‍👩‍👧‍👦 👨‍👩‍👦‍👦 👨‍👩‍👧‍👧 👨‍👨‍👦 👨‍👨‍👧 👨‍👨‍👧‍👦 👨‍👨‍👦‍👦 👨‍👨‍👧‍👧 👩‍👩‍👦 👩‍👩‍👧 👩‍👩‍👧‍👦 👩‍👩‍👦‍👦 👩‍👩‍👧‍👧 👨‍👦 👨‍👦‍👦 👨‍👧 👨‍👧‍👦 👨‍👧‍👧 👩‍👦 👩‍👦‍👦 👩‍👧 👩‍👧‍👦 👩‍👧‍👧 🗣️ 👤 👥 🫂 👪 🧑‍🧑‍🧒 🧑‍🧑‍🧒‍🧒 🧑‍🧒 🧑‍🧒‍🧒 👣 🫆"),
    ("emoji_category_animals", "🐵 🐒 🦍 🦧 🐶 🐕 🦮 🐕‍🦺 🐩 🐺 🦊 🦝 🐱 🐈 🐈‍⬛ 🦁 🐯 🐅 🐆 🐴 🫎 🫏 🐎 🦄 🦓 🦌 🦬 🐮 🐂 🐃 🐄 🐷 🐖 🐗 🐽 🐏 🐑 🐐 🐪 🐫 🦙 🦒 🐘 🦣 🦏 🦛 🐭 🐁 🐀 🐹 🐰 🐇 🐿️ 🦫 🦔 🦇 🐻 🐻‍❄️ 🐨 🐼 🦥 🦦 🦨 🦘 🦡 🐾 🦃 🐔 🐓 🐣 🐤 🐥 🐦 🐧 🕊️ 🦅 🦆 🦢 🦉 🦤 🪶 🦩 🦚 🦜 🪽 🐦‍⬛ 🪿 🐦‍🔥 🐸 🐊 🐢 🦎 🐍 🐲 🐉 🦕 🦖 🐳 🐋 🐬 🫍 🦭 🐟 🐠 🐡 🦈 🐙 🐚 🪸 🪼 🦀 🦞 🦐 🦑 🦪 🐌 🦋 🐛 🐜 🐝 🪲 🐞 🦗 🪳 🕷️ 🕸️ 🦂 🦟 🪰 🪱 🦠 💐 🌸 💮 🪷 🏵️ 🌹 🥀 🌺 🌻 🌼 🌷 🪻 🌱 🪴 🌲 🌳 🌴 🌵 🌾 🌿 ☘️ 🍀 🍁 🍂 🍃 🪹 🪺 🍄 🪾"),
    ("emoji_category_food", "🍇 🍈 🍉 🍊 🍋 🍋‍🟩 🍌 🍍 🥭 🍎 🍏 🍐 🍑 🍒 🍓 🫐 🥝 🍅 🫒 🥥 🥑 🍆 🥔 🥕 🌽 🌶️ 🫑 🥒 🥬 🥦 🧄 🧅 🥜 🫘 🌰 🫚 🫛 🍄‍🟫 🫜 🍞 🥐 🥖 🫓 🥨 🥯 🥞 🧇 🧀 🍖 🍗 🥩 🥓 🍔 🍟 🍕 🌭 🥪 🌮 🌯 🫔 🥙 🧆 🥚 🍳 🥘 🍲 🫕 🥣 🥗 🍿 🧈 🧂 🥫 🍱 🍘 🍙 🍚 🍛 🍜 🍝 🍠 🍢 🍣 🍤 🍥 🥮 🍡 🥟 🥠 🥡 🍦 🍧 🍨 🍩 🍪 🎂 🍰 🧁 🥧 🍫 🍬 🍭 🍮 🍯 🍼 🥛 ☕ 🫖 🍵 🍶 🍾 🍷 🍸 🍹 🍺 🍻 🥂 🥃 🫗 🥤 🧋 🧃 🧉 🧊 🥢 🍽️ 🍴 🥄 🔪 🫙 🏺"),
    ("emoji_category_activities", "🎃 🎄 🎆 🎇 🧨 ✨ 🎈 🎉 🎊 🎋 🎍 🎎 🎏 🎐 🎑 🧧 🎀 🎁 🎗️ 🎟️ 🎫 🎖️ 🏆 🏅 🥇 🥈 🥉 ⚽ ⚾ 🥎 🏀 🏐 🏈 🏉 🎾 🥏 🎳 🏏 🏑 🏒 🥍 🏓 🏸 🥊 🥋 🥅 ⛳ ⛸️ 🎣 🤿 🎽 🎿 🛷 🥌 🎯 🪀 🪁 🔫 🎱 🔮 🪄 🎮 🕹️ 🎰 🎲 🧩 🧸 🪅 🪩 🪆 ♠️ ♥️ ♦️ ♣️ ♟️ 🃏 🀄 🎴 🎭 🖼️ 🎨 🧵 🪡 🧶 🪢"),
    ("emoji_category_travel", "🌍 🌎 🌏 🌐 🗺️ 🗾 🧭 🏔️ ⛰️ 🛘 🌋 🗻 🏕️ 🏖️ 🏜️ 🏝️ 🏞️ 🏟️ 🏛️ 🏗️ 🧱 🪨 🪵 🛖 🏘️ 🏚️ 🏠 🏡 🏢 🏣 🏤 🏥 🏦 🏨 🏩 🏪 🏫 🏬 🏭 🏯 🏰 💒 🗼 🗽 ⛪ 🕌 🛕 🕍 ⛩️ 🕋 ⛲ ⛺ 🌁 🌃 🏙️ 🌄 🌅 🌆 🌇 🌉 ♨️ 🎠 🛝 🎡 🎢 💈 🎪 🚂 🚃 🚄 🚅 🚆 🚇 🚈 🚉 🚊 🚝 🚞 🚋 🚌 🚍 🚎 🚐 🚑 🚒 🚓 🚔 🚕 🚖 🚗 🚘 🚙 🛻 🚚 🚛 🚜 🏎️ 🏍️ 🛵 🦽 🦼 🛺 🚲 🛴 🛹 🛼 🚏 🛣️ 🛤️ 🛢️ ⛽ 🛞 🚨 🚥 🚦 🛑 🚧 ⚓ 🛟 ⛵ 🛶 🚤 🛳️ ⛴️ 🛥️ 🚢 ✈️ 🛩️ 🛫 🛬 🪂 💺 🚁 🚟 🚠 🚡 🛰️ 🚀 🛸 🛎️ 🧳 ⌛ ⏳ ⌚ ⏰ ⏱️ ⏲️ 🕰️ 🕛 🕧 🕐 🕜 🕑 🕝 🕒 🕞 🕓 🕟 🕔 🕠 🕕 🕡 🕖 🕢 🕗 🕣 🕘 🕤 🕙 🕥 🕚 🕦 🌑 🌒 🌓 🌔 🌕 🌖 🌗 🌘 🌙 🌚 🌛 🌜 🌡️ ☀️ 🌝 🌞 🪐 ⭐ 🌟 🌠 🌌 ☁️ ⛅ ⛈️ 🌤️ 🌥️ 🌦️ 🌧️ 🌨️ 🌩️ 🌪️ 🌫️ 🌬️ 🌀 🌈 🌂 ☂️ ☔ ⛱️ ⚡ ❄️ ☃️ ⛄ ☄️ 🔥 💧 🌊"),
    ("emoji_category_objects", "👓 🕶️ 🥽 🥼 🦺 👔 👕 👖 🧣 🧤 🧥 🧦 👗 👘 🥻 🩱 🩲 🩳 👙 👚 🪭 👛 👜 👝 🛍️ 🎒 🩴 👞 👟 🥾 🥿 👠 👡 🩰 👢 🪮 👑 👒 🎩 🎓 🧢 🪖 ⛑️ 📿 💄 💍 💎 🔇 🔈 🔉 🔊 📢 📣 📯 🔔 🔕 🎼 🎵 🎶 🎙️ 🎚️ 🎛️ 🎤 🎧 📻 🎷 🎺 🪊 🪗 🎸 🎹 🎻 🪕 🥁 🪘 🪇 🪈 🪉 📱 📲 ☎️ 📞 📟 📠 🔋 🪫 🔌 💻 🖥️ 🖨️ ⌨️ 🖱️ 🖲️ 💽 💾 💿 📀 🧮 🎥 🎞️ 📽️ 🎬 📺 📷 📸 📹 📼 🔍 🔎 🕯️ 💡 🔦 🏮 🪔 📔 📕 📖 📗 📘 📙 📚 📓 📒 📃 📜 📄 📰 🗞️ 📑 🔖 🏷️ 🪙 💰 🪎 💴 💵 💶 💷 💸 💳 🧾 💹 ✉️ 📧 📨 📩 📤 📥 📦 📫 📪 📬 📭 📮 🗳️ ✏️ ✒️ 🖋️ 🖊️ 🖌️ 🖍️ 📝 💼 📁 📂 🗂️ 📅 📆 🗒️ 🗓️ 📇 📈 📉 📊 📋 📌 📍 📎 🖇️ 📏 📐 ✂️ 🗃️ 🗄️ 🗑️ 🔒 🔓 🔏 🔐 🔑 🗝️ 🔨 🪓 ⛏️ ⚒️ 🛠️ 🗡️ ⚔️ 💣 🪃 🏹 🛡️ 🪚 🔧 🪛 🔩 ⚙️ 🗜️ ⚖️ 🦯 🔗 ⛓️‍💥 ⛓️ 🪝 🧰 🧲 🪜 🪏 ⚗️ 🧪 🧫 🧬 🔬 🔭 📡 💉 🩸 💊 🩹 🩼 🩺 🩻 🚪 🛗 🪞 🪟 🛏️ 🛋️ 🪑 🚽 🪠 🚿 🛁 🪤 🪒 🧴 🧷 🧹 🧺 🧻 🪣 🧼 🫧 🪥 🧽 🧯 🛒 🚬 ⚰️ 🪦 ⚱️ 🧿 🪬 🗿 🪧 🪪"),
    ("emoji_category_symbols", "🏧 🚮 🚰 ♿ 🚹 🚺 🚻 🚼 🚾 🛂 🛃 🛄 🛅 ⚠️ 🚸 ⛔ 🚫 🚳 🚭 🚯 🚱 🚷 📵 🔞 ☢️ ☣️ ⬆️ ↗️ ➡️ ↘️ ⬇️ ↙️ ⬅️ ↖️ ↕️ ↔️ ↩️ ↪️ ⤴️ ⤵️ 🔃 🔄 🔙 🔚 🔛 🔜 🔝 🛐 ⚛️ 🕉️ ✡️ ☸️ ☯️ ✝️ ☦️ ☪️ ☮️ 🕎 🔯 🪯 ♈ ♉ ♊ ♋ ♌ ♍ ♎ ♏ ♐ ♑ ♒ ♓ ⛎ 🔀 🔁 🔂 ▶️ ⏩ ⏭️ ⏯️ ◀️ ⏪ ⏮️ 🔼 ⏫ 🔽 ⏬ ⏸️ ⏹️ ⏺️ ⏏️ 🎦 🔅 🔆 📶 🛜 📳 📴 ♀️ ♂️ ⚧️ ✖️ ➕ ➖ ➗ 🟰 ♾️ ‼️ ⁉️ ❓ ❔ ❕ ❗ 〰️ 💱 💲 ⚕️ ♻️ ⚜️ 🔱 📛 🔰 ⭕ ✅ ☑️ ✔️ ❌ ❎ ➰ ➿ 〽️ ✳️ ✴️ ❇️ ©️ ®️ ™️ 🫟 #️⃣ *️⃣ 0️⃣ 1️⃣ 2️⃣ 3️⃣ 4️⃣ 5️⃣ 6️⃣ 7️⃣ 8️⃣ 9️⃣ 🔟 🔠 🔡 🔢 🔣 🔤 🅰️ 🆎 🅱️ 🆑 🆒 🆓 ℹ️ 🆔 Ⓜ️ 🆕 🆖 🅾️ 🆗 🅿️ 🆘 🆙 🆚 🈁 🈂️ 🈷️ 🈶 🈯 🉐 🈹 🈚 🈲 🉑 🈸 🈴 🈳 ㊗️ ㊙️ 🈺 🈵 🔴 🟠 🟡 🟢 🔵 🟣 🟤 ⚫ ⚪ 🟥 🟧 🟨 🟩 🟦 🟪 🟫 ⬛ ⬜ ◼️ ◻️ ◾ ◽ ▪️ ▫️ 🔶 🔷 🔸 🔹 🔺 🔻 💠 🔘 🔳 🔲"),
    ("emoji_category_flags", "🏁 🚩 🎌 🏴 🏳️ 🏳️‍🌈 🏳️‍⚧️ 🏴‍☠️ 🇦🇨 🇦🇩 🇦🇪 🇦🇫 🇦🇬 🇦🇮 🇦🇱 🇦🇲 🇦🇴 🇦🇶 🇦🇷 🇦🇸 🇦🇹 🇦🇺 🇦🇼 🇦🇽 🇦🇿 🇧🇦 🇧🇧 🇧🇩 🇧🇪 🇧🇫 🇧🇬 🇧🇭 🇧🇮 🇧🇯 🇧🇱 🇧🇲 🇧🇳 🇧🇴 🇧🇶 🇧🇷 🇧🇸 🇧🇹 🇧🇻 🇧🇼 🇧🇾 🇧🇿 🇨🇦 🇨🇨 🇨🇩 🇨🇫 🇨🇬 🇨🇭 🇨🇮 🇨🇰 🇨🇱 🇨🇲 🇨🇳 🇨🇴 🇨🇵 🇨🇶 🇨🇷 🇨🇺 🇨🇻 🇨🇼 🇨🇽 🇨🇾 🇨🇿 🇩🇪 🇩🇬 🇩🇯 🇩🇰 🇩🇲 🇩🇴 🇩🇿 🇪🇦 🇪🇨 🇪🇪 🇪🇬 🇪🇭 🇪🇷 🇪🇸 🇪🇹 🇪🇺 🇫🇮 🇫🇯 🇫🇰 🇫🇲 🇫🇴 🇫🇷 🇬🇦 🇬🇧 🇬🇩 🇬🇪 🇬🇫 🇬🇬 🇬🇭 🇬🇮 🇬🇱 🇬🇲 🇬🇳 🇬🇵 🇬🇶 🇬🇷 🇬🇸 🇬🇹 🇬🇺 🇬🇼 🇬🇾 🇭🇰 🇭🇲 🇭🇳 🇭🇷 🇭🇹 🇭🇺 🇮🇨 🇮🇩 🇮🇪 🇮🇱 🇮🇲 🇮🇳 🇮🇴 🇮🇶 🇮🇷 🇮🇸 🇮🇹 🇯🇪 🇯🇲 🇯🇴 🇯🇵 🇰🇪 🇰🇬 🇰🇭 🇰🇮 🇰🇲 🇰🇳 🇰🇵 🇰🇷 🇰🇼 🇰🇾 🇰🇿 🇱🇦 🇱🇧 🇱🇨 🇱🇮 🇱🇰 🇱🇷 🇱🇸 🇱🇹 🇱🇺 🇱🇻 🇱🇾 🇲🇦 🇲🇨 🇲🇩 🇲🇪 🇲🇫 🇲🇬 🇲🇭 🇲🇰 🇲🇱 🇲🇲 🇲🇳 🇲🇴 🇲🇵 🇲🇶 🇲🇷 🇲🇸 🇲🇹 🇲🇺 🇲🇻 🇲🇼 🇲🇽 🇲🇾 🇲🇿 🇳🇦 🇳🇨 🇳🇪 🇳🇫 🇳🇬 🇳🇮 🇳🇱 🇳🇴 🇳🇵 🇳🇷 🇳🇺 🇳🇿 🇴🇲 🇵🇦 🇵🇪 🇵🇫 🇵🇬 🇵🇭 🇵🇰 🇵🇱 🇵🇲 🇵🇳 🇵🇷 🇵🇸 🇵🇹 🇵🇼 🇵🇾 🇶🇦 🇷🇪 🇷🇴 🇷🇸 🇷🇺 🇷🇼 🇸🇦 🇸🇧 🇸🇨 🇸🇩 🇸🇪 🇸🇬 🇸🇭 🇸🇮 🇸🇯 🇸🇰 🇸🇱 🇸🇲 🇸🇳 🇸🇴 🇸🇷 🇸🇸 🇸🇹 🇸🇻 🇸🇽 🇸🇾 🇸🇿 🇹🇦 🇹🇨 🇹🇩 🇹🇫 🇹🇬 🇹🇭 🇹🇯 🇹🇰 🇹🇱 🇹🇲 🇹🇳 🇹🇴 🇹🇷 🇹🇹 🇹🇻 🇹🇼 🇹🇿 🇺🇦 🇺🇬 🇺🇲 🇺🇳 🇺🇸 🇺🇾 🇺🇿 🇻🇦 🇻🇨 🇻🇪 🇻🇬 🇻🇮 🇻🇳 🇻🇺 🇼🇫 🇼🇸 🇽🇰 🇾🇪 🇾🇹 🇿🇦 🇿🇲 🇿🇼 🏴󠁧󠁢󠁥󠁮󠁧󠁿 🏴󠁧󠁢󠁳󠁣󠁴󠁿 🏴󠁧󠁢󠁷󠁬󠁳󠁿"),
)

# Extra everyday terms make the search useful in every shipped language;
# Unicode already supplies the complete English names.  Accents are removed
# during matching, so both "coração" and "coracao" work.
EMOJI_SEARCH_ALIASES = {
    "😀 😃 😄 😁 😂 😊 😍 🥰 😘 😎 😢 😭 😡 🙂 🙃 😉 😌 🤩 🥳 😏 😴 🤗 🤔 🤭 🤫 😐 🙄 😮 😱 🤢 🤧 😇 🤠":
        "face rosto cara carinha emoji emoção emocao expressão expressao",
    "❤️ 🧡 💛 💚 💙 💜 🖤 🤍 🤎 💔 ❣️ 💕 💞":
        "heart hearts love amor coração coracao corazón corazon serce miłość milosc",
    "😀 😃 😄 😁 😊 🙂 😇":
        "smile smiling happy grin sorriso sorrir feliz sonrisa sonreír sonreir uśmiech usmiech szczęśliwy szczesliwy",
    "😂 🤣": "laugh laughing lol rir riso gargalhada reír reir risa śmiech smiech",
    "😢 😭": "sad crying cry triste chorar lágrima lagrima llorar llanto płacz placz smutny",
    "😡": "angry mad bravo raiva enojado enfadado zły zly",
    "👍": "thumb up like joinha curtir pulgar arriba kciuk góra gora",
    "👎": "thumb down dislike não nao gostei pulgar abajo kciuk dół dol",
    "🙏": "pray prayer thanks obrigado obrigada gracias dziękuję dziekuje",
    "🎉 🥳": "party celebration festa comemorar fiesta celebración celebracion impreza święto swieto",
    "🔥": "fire flame hot fogo chama calor fuego ogień ogien",
    "🐶": "dog puppy cachorro cão cao perro pies",
    "🐱": "cat kitten gato gata kot",
    "🍕": "pizza",
    "🍔": "burger hamburger hambúrguer hamburguer",
    "☕": "coffee café cafe kawa",
    "⚽": "football soccer futebol fútbol futbol piłka pilka",
    "🎵 🎤 🎧 🎸 🎹": "music música musica song som canción cancion muzyka",
    "🚗": "car automobile carro coche samochód samochod",
    "✈️": "airplane plane travel avião aviao viaje avión avion samolot podróż podroz",
    "🏠": "home house casa hogar dom",
    "📱": "phone mobile celular telefone teléfono telefono telefon",
    "🎁": "gift present presente regalo prezent",
    "✅ ✔️": "check correct done certo concluído concluido correcto gotowe",
    "❌": "cross wrong error errado cancelar incorrecto błąd blad",
    "❓": "question dúvida duvida pregunta pytanie",
    "❗ ⚠️": "warning alert attention atenção atencao alerta atención atencion ostrzeżenie ostrzezenie",
    "🙃": "upside down invertido cabeça para baixo cabeca de ponta cabeza abajo",
    "😉": "wink winking piscadela piscar guiño guino",
    "😌": "relieved calm aliviado calma tranquilo alivio",
    "😍 🥰": "love in love apaixonado apaixonada carinho enamorado corazones",
    "😘": "kiss kissing beijo beijando beso",
    "😎": "sunglasses cool óculos oculos escuro gafas sol",
    "🤩": "star eyes estrela olhos estrelas admirado",
    "😏": "smirk malicioso convencido sorriso maroto",
    "😴": "sleep sleeping sleepy sono dormir dormindo sueño sueno",
    "🤗": "hug hugging abraço abraco abraçando abrazando",
    "🤔": "think thinking pensando dúvida duvida pensativo",
    "🤭": "giggle hand mouth mão boca mao rindo escondido",
    "🤫": "quiet silence shush silêncio silencio calado secreto",
    "😐": "neutral expressionless neutro sem expressão expressao",
    "🙄": "rolling eyes revirando olhos impaciente",
    "😮": "surprised open mouth surpresa boca aberta",
    "😱": "scream fear afraid grito medo assustado",
    "🤢": "sick nausea nauseated doente enjoo enjoado náusea nausea",
    "🤧": "sneeze sneezing espirro espirrando resfriado",
    "😇": "angel halo anjo auréola aureola inocente",
    "🤠": "cowboy caubói cauboi chapéu chapeu",
    "👋": "wave waving hello goodbye acenar tchau olá ola saludo",
    "🤚 🖐️ ✋": "raised hand mão levantada mao aberta pare alto",
    "🖖": "vulcan prosper vida longa saudação saudacao",
    "👌": "ok perfect perfeito certo",
    "🤌": "pinched fingers dedos juntos italiano",
    "🤏": "pinching small pouco pequeno",
    "✌️": "victory peace vitória vitoria paz dois dedos",
    "🤞": "crossed fingers sorte dedos cruzados",
    "🤟": "love you amo linguagem sinais",
    "🤘": "rock horns metal chifres",
    "🤙": "call me ligar telefone mão mao",
    "👈": "left apontar esquerda dedo",
    "👉": "right apontar direita dedo",
    "👆 ☝️": "up apontar cima dedo",
    "👇": "down apontar baixo dedo",
    "✊ 👊 🤛 🤜": "fist punch punho soco força forca",
    "👏": "clap applause palmas aplauso aplaudir",
    "🙌": "raised hands celebrate mãos maos levantadas comemorar",
    "👐": "open hands mãos maos abertas",
    "🤝": "handshake acordo aperto mãos maos parceria",
    "💪": "muscle strong strength músculo musculo forte força forca",
    "🐭": "mouse rato ratinho ratón raton",
    "🐹": "hamster",
    "🐰": "rabbit bunny coelho conejo",
    "🦊": "fox raposa zorro",
    "🐻": "bear urso oso",
    "🐼": "panda",
    "🐨": "koala coala",
    "🐯": "tiger tigre",
    "🦁": "lion leão leao león leon",
    "🐮": "cow vaca",
    "🐷": "pig porco porquinho cerdo",
    "🐸": "frog sapo rana",
    "🐵": "monkey macaco mono",
    "🐔": "chicken hen galinha pollo",
    "🐧": "penguin pinguim pingüino pinguino",
    "🐦": "bird pássaro passaro ave pájaro pajaro",
    "🦄": "unicorn unicórnio unicornio",
    "🐝": "bee abelha abeja",
    "🦋": "butterfly borboleta mariposa",
    "🐢": "turtle tartaruga tortuga",
    "🐬": "dolphin golfinho delfín delfin",
    "🍎": "apple maçã maca manzana",
    "🍐": "pear pera",
    "🍊": "orange laranja naranja",
    "🍋": "lemon limão limao limón limon",
    "🍌": "banana",
    "🍉": "watermelon melancia sandía sandia",
    "🍇": "grapes uva uvas",
    "🍓": "strawberry morango fresa",
    "🫐": "blueberry mirtilo arándano arandano",
    "🍒": "cherry cherries cereja cerejas",
    "🍑": "peach pêssego pessego melocotón melocoton",
    "🥭": "mango manga",
    "🍍": "pineapple abacaxi piña pina",
    "🥝": "kiwi",
    "🍅": "tomato tomate",
    "🥑": "avocado abacate aguacate",
    "🍟": "fries chips batata frita papas fritas",
    "🌭": "hot dog cachorro quente",
    "🍿": "popcorn pipoca palomitas",
    "🍩": "donut doughnut rosquinha",
    "🎂": "cake birthday bolo aniversário aniversario cumpleaños cumpleanos",
    "🏀": "basketball basquete baloncesto",
    "🏈": "american football futebol americano",
    "⚾": "baseball beisebol béisbol beisbol",
    "🎾": "tennis tênis tenis",
    "🏐": "volleyball vôlei volei voleibol",
    "🎱": "pool billiards sinuca bilhar",
    "⚽ 🏀 🏈 ⚾ 🎾 🏐 🎱 🏓 🏸": "ball bola esporte jogo",
    "🏓": "ping pong table tennis tênis mesa tenis",
    "🏸": "badminton peteca",
    "🥅": "goal net gol rede",
    "🏆": "trophy champion troféu trofeu campeão campeao copa",
    "🥇": "gold medal first medalha ouro primeiro",
    "🎮": "game videogame controller jogo controle",
    "🎯": "target dart alvo dardo",
    "🎲": "dice dado sorte",
    "🎸": "guitar guitarra violão violao",
    "🎹": "piano keyboard teclado musical",
    "🎤": "microphone mic microfone cantar",
    "🎧": "headphones fone ouvido auscultadores",
    "🎬": "movie cinema film filme claquete",
    "🎨": "art paint palette arte pintura paleta",
    "🚕": "taxi táxi",
    "🚌": "bus ônibus onibus autocarro autobús autobus",
    "🚑": "ambulance ambulância ambulancia",
    "🚒": "fire truck bombeiro caminhão caminhao incêndio incendio",
    "🚲": "bicycle bike bicicleta bici",
    "🏍️": "motorcycle motorbike moto motocicleta",
    "🚀": "rocket foguete cohete espaço espaco",
    "🚁": "helicopter helicóptero helicoptero",
    "⛵": "sailboat barco vela velero",
    "🚢": "ship navio barco buque",
    "🗺️": "map mapa mundo",
    "🏖️": "beach praia playa",
    "🏕️": "camping campsite acampamento campamento",
    "🏢": "office building prédio predio escritório escritorio edificio",
    "🏥": "hospital saúde saude médico medico",
    "🏫": "school escola colegio",
    "🌍 🌎 🌏": "earth world globe terra mundo planeta globo",
    "⌚": "watch clock relógio relogio pulso",
    "💻": "laptop computer notebook computador",
    "⌨️": "keyboard teclado",
    "🖥️": "desktop monitor computer computador tela ecrã ecra",
    "🖨️": "printer impressora",
    "📷": "camera photo câmera camera foto",
    "🎥": "video camera câmera filmar",
    "📺": "television tv televisão televisao",
    "📻": "radio rádio",
    "🔔": "bell notification sino notificação notificacao campainha",
    "🔕": "muted bell silent notification sino mudo silencioso",
    "💡": "light bulb idea lâmpada lampada ideia luz",
    "🔦": "flashlight lanterna",
    "📚": "books livros biblioteca estudiar estudar",
    "✏️": "pencil lápis lapis escrever",
    "📝": "memo note anotação anotacao nota escrever",
    "📌": "pin pushpin alfinete marcador",
    "📎": "paperclip clipe anexo",
    "🔒": "lock locked cadeado fechado segurança seguranca",
    "🔑": "key chave llave senha",
    "💔": "broken heart coração partido coracao triste desamor",
    "💕 💞": "hearts love corações coracoes amor carinho",
    "💯": "hundred perfect cem perfeito nota máxima maxima",
    "➕": "plus add positive mais adicionar soma",
    "➖": "minus subtract negative menos subtrair",
    "♻️": "recycle recycling reciclar reciclagem",
    "🏁": "finish racing flag chegada corrida bandeira",
    "🚩": "red flag bandeira vermelha alerta",
    "🏳️‍🌈": "rainbow flag pride bandeira arco íris iris orgulho lgbt",
    "🏳️‍⚧️": "transgender flag bandeira trans orgulho",
    "🇧🇷": "brazil brasil brasileiro bandeira",
    "🇵🇹": "portugal português portugues bandeira",
    "🇺🇸": "united states usa estados unidos americano bandeira",
    "🇪🇸": "spain espanha españa bandeira",
    "🇵🇱": "poland polônia polonia polska bandeira",
    "🇦🇷": "argentina bandeira",
    "🇲🇽": "mexico méxico bandeira",
    "🇨🇦": "canada canadá bandeira",
    "🇬🇧": "united kingdom uk britain reino unido inglaterra bandeira",
    "🇫🇷": "france frança franca bandeira",
    "🇩🇪": "germany alemanha deutschland bandeira",
    "🇮🇹": "italy itália italia bandeira",
    "🇯🇵": "japan japão japao bandeira",
}

# >>> EMOJI_CLDR_KEYWORDS (generated; do not edit by hand)
# Official CLDR annotation keywords (pt-BR only), accent-folded and
# lowercased; family members roll up into their displayed row.
EMOJI_CLDR_KEYWORDS = {
    "😀": "rosto risonho engracado feliz lol rindo risada riso sorridente",
    "😃": "rosto risonho olhos bem abertos aberto boca feliz arregalados risada sorrindo aberta sorridente sorriso",
    "😄": "rosto risonho olhos sorridentes aberta boca engracado feliz haha lol risada riso sorridente sorriso",
    "😁": "rosto contente olhos sorridentes feliz olho sorrindo rindo sorridente sorriso aberto",
    "😂": "rosto chorando rir alegria engracada engracado gargalhada hahaha kkk lagrimas",
    "😊": "rosto sorridente olhos sorridentes alegre encabulada envergonhada feliz fofo ruborizar satisfeita sim sorriso vergonha",
    "😍": "rosto sorridente olhos coracao amor apaixonado olhar paixao romance",
    "🥰": "rosto sorridente 3 coracoes amando amei amo apaixonada coracao crush paixao romance te",
    "😘": "rosto mandando beijo flerte jogando beijos",
    "😎": "rosto sorridente oculos escuros muito sol na boa sorrindo sorriso to legal",
    "😢": "rosto chorando choro lagrimas triste",
    "😭": "rosto chorando aos berros alto chorar infeliz lagrimas triste",
    "😡": "rosto furioso bravo irado vermelho zangado",
    "👍": "polegar cima beleza concordo dedao joia mao ok sim sinal valeu",
    "👎": "polegar baixo dedao desaprovacao desaprovado mao nao gostei ruim sinal",
    "❤️": "coracao vermelho amor s2",
    "🎉": "cone festa alegria aniversario celebrar comemoracao eba oba parabens",
    "🙏": "maos juntas gesto mao reza rezando rezar",
    "🔥": "fogo chama",
    "😆": "rosto risonho olhos semicerrados gargalhada gargalhando hahaha kkk fechados rir sorriso estilo xd",
    "😅": "rosto risonho gota suor estressada estressado nervoso rindo sorriso frio mas suando",
    "🤣": "rolando no chao rir choro engracada feliz gargalhada haha kkk lagrimas rindo risada",
    "🙂": "rosto levemente sorridente feliz sorrindo sorriso leve",
    "🙃": "rosto cabeca baixo invertido",
    "🫠": "rosto derretendo calor derreter desaparecer dissolver envergonhada haha quente sarcasmo sarcastica vergonha",
    "😉": "rosto olho piscando flerte piscada",
    "😇": "rosto sorridente aureola anjinho anjo inocente sorriso",
    "🤩": "rosto olhar maravilhado estrela gargalhando olhos super animada animado feliz superanimada superanimado superfeliz",
    "😗": "rosto beijando beijo",
    "☺️": "rosto sorridente corada corado relaxado sorrindo nao preenchido sorriso",
    "😚": "rosto beijando olhos fechados beijinho mandando beijo",
    "😙": "rosto beijando olhos sorridentes beijar sorrir beijinho beijos flerte mandando beijo sorrindo sorriso",
    "🥲": "rosto sorridente lagrima aliviada chorar contente emocionado felicidade feliz grata gratidao orgulhosa rir sorrir",
    "😋": "rosto saboreando comida apetitoso gostosa delicioso gostoso nham sorriso saboroso",
    "😛": "rosto mostrando lingua fora",
    "😜": "rosto piscando lingua fora brincadeira piscadela mostrando provocacao",
    "🤪": "rosto bizarro cara maluco doida doido excentrico grande louca louco olhar olho pequeno",
    "😝": "rosto olhos semicerrados lingua fora eca horrivel fechados",
    "🤑": "rosto cifroes avarenta caro cifrao dinheiro ganancia grana rica riqueza",
    "🤗": "rosto abracando abraco carinho feliz maos sorrindo",
    "🤭": "rosto mao sobre boca hehe ops risadinha risonho sorriso envergonhado vergonha",
    "🫢": "rosto olhos abertos mao sobre boca assustada chocada descrenca choque espanto nossa omg passada quieta surpresa temor",
    "🫣": "rosto olho espiando cativada cativado envergonhada envergonhado esconder espiadinha espiar olhar fixamente smiley timida timido vergonha",
    "🤫": "rosto fazendo sinal silencio faca fica quieta quieto",
    "🤔": "rosto pensativo curiosa curioso duvida hmm ideia ideias mao no queixo pensando pensativa",
    "🫡": "rosto saudando boa sorte exercito ok respeito saudacao senhor sim tropa",
    "🤐": "rosto boca ziper fechada calada calado quieta quieto segredo",
    "🤨": "rosto sobrancelha levantada cetica ceticismo cetico confusa confuso desconfiada desconfiado desconfianca",
    "😐": "rosto neutro putz sem emocao comentarios reacao",
    "😑": "rosto inexpressivo aff inespressiva inespressivo nada dizer sem comentarios expressao tanto faz",
    "😶": "rosto sem boca calada calado quieto comentarios palavras",
    "🫥": "rosto linha pontilhada deprimida desaparecer esconder escondido indiferenca introvertido invisivel meh tanto faz",
    "😶‍🌫️": "rosto nas nuvens cabeca distraido no nevoeiro",
    "😏": "rosto sorriso maroto flerte maldoso malicioso suspeito",
    "😒": "rosto aborrecido aff blase nada engracado nao achou graca esta feliz quem",
    "🙄": "rosto olhos revirados olhando cima revirando rolando tanto faz virando",
    "😬": "rosto expressando desagrado careta fazendo sem graca",
    "😮‍💨": "rosto exalando alivio assobio assopro cansaco cansada choque exalar exausta fumaca suspiro triste",
    "🤥": "rosto mentiroso mentindo mentira nariz crescendo pinocchio pinoquio",
    "🫨": "rosto tremendo choque louco loucura meu deus panico surpresa susto terremoto vibracao vibrar",
    "🙂‍↔️": "cabeca virando lado outro balancar balancando na horizontal nao negar",
    "🙂‍↕️": "cabeca balancando na vertical confirmacao sim",
    "😌": "rosto aliviado alivio paz zen",
    "😔": "rosto deprimido abatido chateado desanimado pensativo baixo",
    "😪": "rosto sonolento cara sono",
    "🤤": "rosto babando babar salivando",
    "😴": "rosto dormindo boa noite cama cansada cansado cochilo dormir soneca sono zzz",
    "🫩": "rosto olheiras cansaco cansada cansado dorminhoco exausta exausto bolsas embaixo olhos",
    "😷": "rosto mascara medica doente gripado resfriado",
    "🤒": "rosto termometro cama doente febre febril",
    "🤕": "rosto atadura na cabeca acidentado doente ferimento machucado curativos",
    "🤢": "rosto nauseado doente enjoado enjoo nausea vomito",
    "🤮": "rosto vomitando doente nojenta nojento nojo vomito",
    "🤧": "rosto espirrando cama doente espirro gripado resfriado",
    "🥵": "rosto fervendo calor febre febril insolacao lingua fora ofegante quente vermelho suando suor",
    "🥶": "rosto gelado abaixo zero azul congelando congelei frio gelido glacial",
    "🥴": "rosto embriagado alcoolizado bebada bebado boca ondulada embriagada intoxicado olhos tortos tonta tonto",
    "😵": "rosto atordoado acabado doente morto tontura",
    "😵‍💫": "rosto olhos espiral caramba confuso desnorteado eita espirais hipnotizado problema putz tonto tontura uau",
    "🤯": "cabeca explodindo chocada chocado chocante choque incrivel perplexo surpreendente surpresa surpreso",
    "🤠": "rosto chapeu cauboi cara sertanejo vaqueiro",
    "🥳": "rosto festivo animacao aniversario apito chapeu comemoracao comemorar feliz festa lingua sogra parabens viva",
    "🥸": "rosto disfarcado bigode disfarce espiao incognito nariz oculos pessoa sobrancelha",
    "🤓": "rosto cdf esperto estudioso inteligencia inteligente oculos grau sabe tudo sabe-tudo",
    "🧐": "rosto monoculo conservador",
    "😕": "rosto confuso indeciso nao entendi tenho certeza",
    "🫤": "rosto boca cetico confusao decepcao desapontado duvida frustracao indiferenca inseguro meh tanto faz",
    "😟": "rosto preocupado decepcionado",
    "🙁": "rosto meio triste tristeza tristinho",
    "☹️": "rosto descontente decepcao decepcionado insatisfeito desaprovacao triste",
    "😮": "rosto boca aberta boquiaberto empatia pasmo",
    "😯": "rosto surpreso espantado uau",
    "😲": "rosto espantado chocado estupefato totalmente",
    "😳": "rosto ruborizado atordoado deslumbrado horrorizado impressionado vergonha",
    "🫪": "rosto distorcido ansiedade chocado inchado panico surpreso vulneravel",
    "🥺": "rosto implorando olhar cachorrinho olhos grandes perdao favor nao pq triste",
    "🥹": "rosto segurando lagrimas admiracao alegria chorar raiva emocao gratidao iti malia orgulho favor resistir tristeza",
    "😦": "rosto franzido boca aberta assustado decepcionado inesperado uau",
    "😧": "rosto angustiado sofrendo",
    "😨": "rosto amedrontado ansiedade assustado medo preocupado",
    "😰": "rosto ansioso gota suor boca aberta nervoso azul frio suando",
    "😥": "rosto triste mas aliviado decepcionado nao ufa",
    "😱": "rosto gritando medo assustada assustado grito susto temeroso",
    "😖": "rosto perplexo bravo frustrado indignado",
    "😣": "rosto perseverante concentracao concentrado dor cabeca foco perseveranca",
    "😞": "rosto desapontado decepcao desapontamento decepcionado triste",
    "😓": "rosto cabisbaixo gota suor frio suando",
    "😩": "rosto desolado aborrecido cansado cansei decepcionado exausto infeliz perdi",
    "😫": "rosto cansado desesperado exausto",
    "🥱": "rosto bocejando bocejo cansada cansado sono entediada entediado zzz",
    "😤": "rosto soltando vapor pelo nariz brava bravo furiosa furioso irritada irritado fumaca triunfo vitoria",
    "😠": "rosto zangado irado",
    "🤬": "rosto simbolos na boca censurado fala mal rude xingando",
    "😈": "rosto sorridente chifres cara diabinho diabo malicia roxa sorriso",
    "👿": "rosto zangado chifres bravo demonio diabinho diabo",
    "💀": "caveira conto corpo fadas morte rosto",
    "☠️": "caveira ossos cruzados morte pirata",
    "💩": "coco estrume excremento fezes pilha",
    "🤡": "rosto palhaco cara circo engracado piada",
    "👹": "ogro assustador conto fadas demoniaco japones malvado mascara monstro oni rosto",
    "👺": "duende japones bravo conto fadas mascara monstro raiva rosto tengu zangado",
    "👻": "fantasma assombracao assombrado buu conto fadas halloween rosto",
    "👽": "alienigena extraterrestre ovni rosto",
    "👾": "monstro alienigena e.t extraterrestre game invasores ovni pixelado",
    "🤖": "rosto robo monstro robotizado",
    "😺": "rosto gato sorrindo aberta animal boca feliz sorriso",
    "😸": "rosto gato sorrindo olhos sorridentes rindo riso sorriso",
    "😹": "rosto gato lagrimas alegria chorando choro engracado felicidade feliz rir risos",
    "😻": "rosto gato sorridente olhos coracao adorei amor apaixonado paixao sorriso",
    "😼": "rosto gato sorriso ironico",
    "😽": "rosto gato mandando beijo beijando fechado olho",
    "🙀": "rosto gato desolado choque meu deus oh! surpresa surpreso",
    "😿": "rosto gato chorando choro lagrima triste",
    "😾": "rosto gato mal-humorado bico bravo",
    "🙈": "macaco nao ve nada envergonhado veja mal vi olhos tapados vergonha",
    "🙉": "macaco nao ouve nada ouca mal ouco quero ouvir ouvidos tapados",
    "🙊": "macaco nao fala nada animal boca tapada fale posso contar falar segredo silencio ups",
    "💌": "carta amor coracao correspondencia romance",
    "💘": "coracao flecha amor cupido emocao flechado paixao romance s2",
    "💝": "coracao fita amor aniversario dia namorados presente s2",
    "💖": "coracao brilhante amor emocao emocionante s2",
    "💗": "coracao crescendo amor animado batendo emocao nervosismo s2",
    "💓": "coracao pulsante amor batendo emocao s2",
    "💞": "coracoes girando adoravel amor bonitinho coracao emocao s2",
    "💕": "dois coracoes amor aniversario casal coracao emocao s2",
    "💟": "coracao decorativo amor decoracao roxo s2 selo",
    "❣️": "exclamacao coracao amor pontuacao s2 sinal",
    "💔": "coracao partido amorosa decepcao desilusao emocao quebrado rompimento s2 sofrendo sofrimento triste",
    "❤️‍🔥": "coracao chamas amor sagrado fogo luxuria",
    "❤️‍🩹": "coracao remendado bem bom curando mais saudavel melhorando recuperacao",
    "🩷": "coracao rosa adoravel adorei amo voce amor curtir emocao especial fofo gostar gostei te",
    "🧡": "coracao laranja emocao s2",
    "💛": "coracao amarelo amor emocao love s2",
    "💚": "coracao verde emocao s2",
    "💙": "coracao azul emocao s2",
    "🩵": "coracao azul-claro adorar adorei amo voce amor emocao especial fofo gostar gostei te",
    "💜": "coracao roxo lilas emocao s2",
    "🤎": "coracao marrom s2",
    "🖤": "coracao preto negro s2",
    "🩶": "coracao cinza adorar adorei amo voce amor emocao especial gostar gostei te",
    "🤍": "coracao branco s2",
    "💋": "marca beijo labios romance",
    "💯": "cem pontos 100 certamente certeza conte comigo sem duvida top total",
    "💢": "simbolo raiva emocao engracado",
    "🫯": "nuvem briga debate desacordo discussao luta tumulto",
    "💥": "colisao emocao engracado explosao simbolo",
    "💫": "zonzo brilhante emocao engracado estrelas olhando",
    "💦": "pingos suor borrifo emocao engracado splash",
    "💨": "rapidez correr corrida emocao engracado fugir",
    "🕳️": "buraco no chao",
    "💬": "balao dialogo conversa engracado",
    "👁️‍🗨️": "olho no balao dialogo testemunha",
    "🗨️": "balao dialogo esquerda azul conversa",
    "🗯️": "balao raiva direita briga conversa dialogo discurso discussao energico furioso irado",
    "💭": "balao pensamento engracado ideia invencao inventando pensando",
    "💤": "zzz boa noite cansada cansado sono dormindo dormir emocao engracado roncando",
    "👋": "mao acenando aceno ate mais esta ai? flw oi ola tai? tchau",
    "🤚": "dorso mao levantado levantada tudo pare",
    "🖐️": "mao aberta dedos separados 5 cinco palma pare",
    "✋": "mao levantada 5 cinco erguida high five papel pare toca aqui",
    "🖖": "saudacao vulcana dedos jornada nas estrelas mao star trek spock startrek",
    "🫱": "mao direita apertar aperto pegar segurar",
    "🫲": "mao esquerda apertar pegar segurar",
    "🫳": "mao palma baixo cair derrubar descartar pegar segurar soltar xo",
    "🫴": "mao palma cima acenar levantar oferecer pegar segurar venha",
    "🫷": "mao empurrando esquerda bate aqui bloquear empurrar esperar high five parar pausar recusar toca",
    "🫸": "mao empurrando direita bate aqui bloquear empurrar esperar high five parar pausar recusar toca",
    "👌": "sinal beliscar certo concordo mao otimo rude sinalizando",
    "🤌": "dedos comprimidos beliscado coxinha gesto mao ha interrogacao italia italiana maravilha sarcastico",
    "🤏": "mao beliscando beliscar pequena quantidade pequeno pouco pouquinho",
    "✌️": "mao v vitoria beleza paz sim",
    "🤞": "dedos cruzados boa sorte mao torcendo",
    "🫰": "mao dedo indicador polegar cruzados <3 amor army caro coracao dinheiro estalar k-pop kpop",
    "🤟": "gesto amor mao te amo",
    "🤘": "saudacao rock chifres dedos mao metal",
    "🤙": "sinal legal ligar mao liga",
    "👈": "dorso mao indicador apontando esquerda costas dedo",
    "👉": "dorso mao indicador apontando direita apontar costas dedo",
    "👆": "dorso mao indicador apontando cima costas dedo",
    "🖕": "dedo meio gesto ofensivo mao",
    "👇": "dorso mao indicador apontando baixo abaixo costas dedo apontado embaixo",
    "☝️": "indicador apontando cima dedo eu mao",
    "🫵": "indicador apontando visualizador ai apontar cutucar dedo mao tu voce",
    "✊": "punho levantado pedra erguido solidariedade",
    "👊": "soco beleza fechado forca mao punho ta ligado",
    "🤛": "punho esquerdo fechada mao soco",
    "🤜": "punho direito soco",
    "👏": "maos aplaudindo aplauso bom trabalho palmas parabens sinal",
    "🙌": "maos cima ambas comemoracao levantando comemorar viva",
    "🫶": "maos coracao <3 amei amo voce amor mao te",
    "👐": "maos abertas mao sinal",
    "🤲": "palmas unidas cima devocao juntas oracao",
    "🤝": "aperto maos combinado cumprimento",
    "✍️": "escrevendo mao caneta",
    "💅": "esmalte unha cosmeticos manicure mao unhas",
    "🤳": "camera celular foto smartphone",
    "💪": "academia contraido engracado forte musculacao musculo",
    "🦾": "braco mecanico acessibilidade protese",
    "🦿": "perna mecanica acessibilidade protese",
    "🦵": "perna chute joelho membro pe dobrada",
    "🦶": "pe calcanhar chutar chute pisao pisar tornozelo",
    "👂": "orelha corpo escutar ouvido",
    "🦻": "ouvido aparelho auditivo acessibilidade deficiencia auditiva surda surdo",
    "👃": "nariz cheirar cheiro corpo odor rosto",
    "🧠": "cerebro inteligencia inteligente",
    "🫀": "coracao humano anatomia batimento cardiaco cardiologia centro vermelho orgao pulsacao pulso s2",
    "🫁": "pulmoes anatomia espiracao exalacao inalacao orgao pulmao respiracao",
    "🦷": "dente branco dentista",
    "🦴": "osso cachorro esqueleto",
    "👀": "olhos olhando olho rosto to",
    "👁️": "olho parte corpo rosto",
    "👅": "lingua corpo rosto",
    "👄": "boca batom beijo corpo labios rosto",
    "🫦": "mordendo labio ansioso batom beijo flertar labios medo mordida nervoso preocupacao seducao sexy vontade",
    "👶": "bebe bebezinho gravida nenem pessoas recem-nascido",
    "🧒": "crianca filho jovem neto pequeno",
    "👦": "menino garoto guri jovem loiro pessoas pia",
    "👧": "menina filha garota menininha neta pessoas",
    "🧑": "pessoa adulta adulto",
    "👱": "pessoa cabelo louro loiro loira loura rosto",
    "👨": "homem adulto amigo irmao marido namorado",
    "🧔": "homem barba barbudo pessoa",
    "🧔‍♂️": "homem barbudo barba",
    "🧔‍♀️": "mulher barba",
    "👨‍🦰": "homem cabelo vermelho adulto amigo irmao marido namorado",
    "👨‍🦱": "homem cabelo cacheado adulto amigo irmao marido namorado",
    "👨‍🦳": "homem cabelo branco adulto amigo irmao marido namorado",
    "👨‍🦲": "homem careca adulto amigo irmao marido namorado",
    "👩": "mulher adulta garota guria loira menina mina",
    "👩‍🦰": "mulher cabelo vermelho adulta garota guria loira menina mina",
    "🧑‍🦰": "pessoa cabelo vermelho adulta adulto",
    "👩‍🦱": "mulher cabelo cacheado adulta garota guria loira menina mina",
    "🧑‍🦱": "pessoa cabelo cacheado adulta adulto",
    "👩‍🦳": "mulher cabelo branco adulta garota guria loira menina mina",
    "🧑‍🦳": "pessoa cabelo branco adulta adulto",
    "👩‍🦲": "mulher careca adulta garota guria loira menina mina",
    "🧑‍🦲": "pessoa careca adulta adulto",
    "👱‍♀️": "mulher cabelo loiro garota guria loira loura menina",
    "👱‍♂️": "homem cabelo loiro louro",
    "🧓": "idoso adulto avo sabio velho vovo",
    "👴": "homem idoso adulto avo careca pessoas vovo",
    "👵": "idosa adulta mulher pessoas velhinha vovo vovozinha",
    "🙍": "franzindo sobrancelha brava bravo chateada chateado testa gesto pessoa triste",
    "🙍‍♂️": "homem franzindo sobrancelha carrancudo chateado desconfiado gesto magoado menino",
    "🙍‍♀️": "mulher franzindo sobrancelha carrancuda desconfiada",
    "🙎": "pessoa fazendo bico beicinho beico careta chateada chateado emburrada emburrado gesto",
    "🙎‍♂️": "homem fazendo bico apontando cara feia gesto homen",
    "🙎‍♀️": "mulher fazendo bico cara feia",
    "🙅": "pessoa fazendo gesto “nao” jeito nenhum mao nao proibido sem chance",
    "🙅‍♂️": "homem fazendo gesto “nao” mao menino nao proibido proibir",
    "🙅‍♀️": "mulher fazendo gesto “nao” mao proibido",
    "🙆": "pessoa fazendo gesto “ok” exercicio mao maos cima",
    "🙆‍♂️": "homem fazendo gesto “ok” cabeca exercicio mao menino nossa",
    "🙆‍♀️": "mulher fazendo gesto “ok” mao",
    "💁": "pessoa palma virada cima ajuda diva divo fala serio fofoca informacoes jogada cabelo mao sarcasmo",
    "💁‍♂️": "homem palma virada cima ajuda fofoca garoto gorjeta guri menino sarcasmo sarcastico",
    "💁‍♀️": "mulher palma virada cima",
    "🙋": "pessoa levantando mao duvida eu sei feliz gesto levantar",
    "🙋‍♂️": "homem levantando mao eu sei gesto menino pedir palavra pergunta voluntario",
    "🙋‍♀️": "mulher levantando mao gesto pedir palavra",
    "🧏": "pessoa surda acessibilidade audicao orelha surdo surdos",
    "🧏‍♂️": "homem surdo",
    "🧏‍♀️": "mulher surda",
    "🙇": "pessoa fazendo reverencia arrependimento desculpa desculpe gesto meditacao perdao respeitosa",
    "🙇‍♂️": "homem fazendo reverencia desculpas gesto pedindo respeito",
    "🙇‍♀️": "mulher fazendo reverencia desculpas desculpe gesto meditacao meditar pedindo respeito",
    "🤦": "pessoa decepcionada como assim decepcao decepcionado desapontamento descrenca inacreditavel meu deus nao acredito possivel dececionada",
    "🤦‍♂️": "homem decepcionado como assim decepcao desapontamento inacreditavel meu deus nao acredito possivel",
    "🤦‍♀️": "mulher decepcionada como assim decepcao desapontamento inacreditavel meu deus nao acredito possivel",
    "🤷": "pessoa dando ombros dar duvida ignorancia indiferenca nao sei quem sabe tanto faz",
    "🤷‍♂️": "homem dando ombros dar duvida indiferenca menino nao sei quem sabe tanto faz",
    "🤷‍♀️": "mulher dando ombros dar duvida garota indiferenca menina nao sei quem sabe tanto faz",
    "🧑‍⚕️": "profissional saude cuidados enfermeira medico terapeuta",
    "👨‍⚕️": "homem profissional saude doutor enfermeiro medico terapeuta",
    "👩‍⚕️": "mulher profissional saude doutora enfermeira garota guria medica menina terapeuta",
    "🧑‍🎓": "aluno estudante graduando",
    "👨‍🎓": "estudante aluno colando grau formando graduacao homem",
    "👩‍🎓": "aluna estudante formanda mulher",
    "🧑‍🏫": "professora na escola instrutora",
    "👨‍🏫": "professor educador homem instrutor mestre",
    "👩‍🏫": "professora instrutora mestra mulher",
    "🧑‍⚖️": "juiz no tribunal balanca",
    "👨‍⚖️": "juiz balanca homem justica",
    "👩‍⚖️": "juiza balanca justica mulher",
    "🧑‍🌾": "agricultor jardineiro rancheiro",
    "👨‍🌾": "fazendeiro agricultor homem jardineiro",
    "👩‍🌾": "fazendeira agricultora jardineira mulher",
    "🧑‍🍳": "chef cozinha cozinheiro",
    "👨‍🍳": "cozinheiro chef homem restaurante",
    "👩‍🍳": "cozinheira chef mulher",
    "🧑‍🔧": "mecanico trabalhando eletricista encanador prestador servico",
    "👨‍🔧": "mecanico eletricista encanador homem prestador servicos",
    "👩‍🔧": "mecanica eletricista encanadora mulher prestadora servicos",
    "🧑‍🏭": "funcionario fabrica industrial montagem trabalhador",
    "👨‍🏭": "operario fabrica homem industria metalurgico trabalhador",
    "👩‍🏭": "operaria fabrica industria mulher trabalhadora",
    "🧑‍💼": "trabalhador escritorio arquiteto colarinho branco gerente negocios",
    "👨‍💼": "funcionario escritorio arquiteto colarinho branco empresario gerente homem",
    "👩‍💼": "funcionaria escritorio arquiteta branco colarinho empresaria gerente mulher",
    "🧑‍🔬": "cientista biologo engenheiro fisico quimico",
    "👨‍🔬": "cientista homem biologo fisico professor quimico",
    "👩‍🔬": "cientista mulher biologa fisica professora quimica",
    "🧑‍💻": "programador desenvolvedor inventor software tecnologo",
    "👨‍💻": "tecnologo codificador computador criador desenvolvedor homem inventor programador software",
    "👩‍💻": "tecnologa codificadora computador criadora desenvolvedora inventora mulher programadora software",
    "🧑‍🎤": "cantor ator entretenimento estrela rock",
    "👨‍🎤": "cantor homem artista ator pop rock",
    "👩‍🎤": "cantora artista atriz mulher pop rock",
    "🧑‍🎨": "artista paleta",
    "👨‍🎨": "artista plastico homem pintor pintura quadros",
    "👩‍🎨": "artista plastica mulher pintura",
    "🧑‍✈️": "piloto aviao",
    "👨‍✈️": "piloto aviao homem",
    "👩‍✈️": "piloto aviao mulher garota guria voando",
    "🧑‍🚀": "astronauta foguete",
    "👨‍🚀": "astronauta homem espaco foguete",
    "👩‍🚀": "astronauta mulher espaco foguete",
    "🧑‍🚒": "bombeiro caminhao bombeiros",
    "👨‍🚒": "bombeiro homem caminhao corpo bombeiros fogo incendio",
    "👩‍🚒": "bombeira caminhao corpo bombeiros fogo incendio mulher",
    "👮": "policial autoridade multar pessoa policia prender",
    "👮‍♂️": "policial homem policia tira",
    "👮‍♀️": "policial mulher autoridade multar policia prender tira",
    "🕵️": "detetive espiao investigador rosto lupa",
    "🕵️‍♂️": "detetive homem espiao investigador",
    "🕵️‍♀️": "detetive mulher espia espionar investigadora investigar",
    "💂": "guarda londres palacio pessoas seguranca",
    "💂‍♂️": "guarda homem seguranca",
    "💂‍♀️": "guarda mulher buckingham londres palacio realeza seguranca",
    "🥷": "assassino furtividade guerra habilidades luta lutador oculto pessoa soldado",
    "👷": "trabalhador construcao civil capacete chapeu construir pessoa",
    "👷‍♂️": "pedreiro construcao homem operario",
    "👷‍♀️": "pedreira capacete construcao contrucao mulher operaria operario pedreiro",
    "🫅": "pessoa coroa monarca nobre princesa principe rainha real realeza rei reino",
    "🤴": "principe realeza rei",
    "👸": "princesa conto coroa fadas fantasia loira menina mulher pessoas rainha",
    "👳": "pessoa turbante",
    "👳‍♂️": "homem turbante",
    "👳‍♀️": "mulher turbante",
    "👲": "homem bone chapeu chines guapimao pessoas",
    "🧕": "mulher veu cabeca hijab lenco",
    "🤵": "pessoa smoking festa gala homem noivo",
    "🤵‍♂️": "homem smoking",
    "🤵‍♀️": "mulher smoking",
    "👰": "pessoa veu casamento noiva pessoas",
    "👰‍♂️": "noivo veu",
    "👰‍♀️": "noiva veu",
    "🤰": "gravida estou gravidez mulher",
    "🫃": "homem gravido barriga cheia cheio comi demais excesso peso gravidez inchado pessoa",
    "🫄": "pessoa gravida barriga cheia cheio comi demais excesso peso gravidez inchada",
    "🤱": "amamentando amamentacao bebe leite mae materno nenem",
    "👩‍🍼": "mulher alimentando bebe amamentacao amamentando amor mae mamae nascido nenem pessoa recem recem-nascido",
    "👨‍🍼": "homem alimentando bebe amamentacao amamentando amor nenem pai papai pessoa recem nascido recem-nascido",
    "🧑‍🍼": "pessoa alimentando bebe amamentacao amamentar homem mae mulher nenem pai recem-nascido",
    "👼": "bebe anjo conto fadas rosto",
    "🎅": "papai noel comemoracao festas natal",
    "🤶": "mamae noel comemoracao natal vovo",
    "🧑‍🎄": "noel chapeu feliz festas gorro mamae natal pessoa",
    "🦸": "super-heroi bem boa bom heroi heroina super-homem superman superpoder",
    "🦸‍♂️": "homem super-heroi bom heroi superpoder",
    "🦸‍♀️": "super-heroina boa heroi heroina mulher superpoder",
    "🦹": "supervilao bandido criminoso mal malvado mau superpoder vilao",
    "🦹‍♂️": "homem supervilao criminoso mal superpoder vilao",
    "🦹‍♀️": "supervila criminosa ma mulher superpoder vila",
    "🧙": "mago bruxa fantasia feiticeira feiticeiro feitico maga magia rpg",
    "🧙‍♂️": "homem mago feiticeiro",
    "🧙‍♀️": "maga bruxa feiticeira",
    "🧚": "fada asas conto encantado fantasia ser",
    "🧚‍♂️": "homem fada",
    "🧚‍♀️": "mulher fada",
    "🧛": "vampiro assustador bruxas dia dracula fantasia halloween monstro sangue terror",
    "🧛‍♂️": "homem vampiro dracula",
    "🧛‍♀️": "mulher vampira",
    "🧜": "pessoa sereia canto cauda contos criatura fada fundo mar oceano ser encantado sirene tridente",
    "🧜‍♂️": "sereio tritao",
    "🧜‍♀️": "sereia",
    "🧝": "elfo duende fantasia legolas magia magico mistico rpg ser encantado",
    "🧝‍♂️": "elfo homem duende",
    "🧝‍♀️": "elfa duende mulher",
    "🧞": "genio desejos fantasia lampada magica mistico",
    "🧞‍♂️": "homem genio",
    "🧞‍♀️": "mulher genio",
    "🧟": "zumbi apocalipse assustador bruxas dia halloween morto-vivo terror",
    "🧟‍♂️": "homem zumbi cazumbi",
    "🧟‍♀️": "mulher zumbi cazumbi",
    "🧌": "conto fadas fantasia gigante monstro ogro",
    "🫈": "criatura peluda criptido eua floresta gigante pe grande pe‐grande peludo sasquatch yeti",
    "💆": "pessoa recebendo massagem facial dor cabeca pele relaxar rosto spa tensao",
    "💆‍♂️": "homem recebendo massagem facial acalmar dor cabeca menino relaxamento relaxar rosto salao tensao tenso",
    "💆‍♀️": "mulher recebendo massagem facial relaxamento",
    "💇": "pessoa cortando cabelo beleza corte salao",
    "💇‍♂️": "homem cortando cabelo barbeiro beleza corte menino salao",
    "💇‍♀️": "mulher cortando cabelo corte salao beleza",
    "🚶": "pessoa andando andar caminhada caminhar passo pedestre perambulando",
    "🚶‍♂️": "homem andando andar caminhar caminhando",
    "🚶‍♀️": "mulher andando andar caminhada caminhando caminhar passeio pedestre perambulando",
    "🚶‍➡️": "pessoa andando frente direita andar caminhada caminhar passo pedestre perambulando",
    "🚶‍♀️‍➡️": "mulher andando frente direita andar caminhada caminhando caminhar passeio pedestre perambulando",
    "🚶‍♂️‍➡️": "homem andando frente direita andar caminhar caminhando",
    "🧍": "pessoa pe",
    "🧍‍♂️": "homem pe",
    "🧍‍♀️": "mulher pe",
    "🧎": "pessoa ajoelhando ajoelhado ajoelhar joelhos pedir favor",
    "🧎‍♂️": "homem ajoelhando ajoelhado",
    "🧎‍♀️": "mulher ajoelhando ajoelhada",
    "🧎‍➡️": "pessoa ajoelhando frente direita ajoelhado ajoelhar joelhos pedir favor",
    "🧎‍♀️‍➡️": "mulher ajoelhando frente direita ajoelhada",
    "🧎‍♂️‍➡️": "homem ajoelhando frente direita ajoelhado",
    "🧑‍🦯": "pessoa bengala cego acessibilidade",
    "🧑‍🦯‍➡️": "pessoa bengala cego frente direita acessibilidade",
    "👨‍🦯": "homem bengala cego accessibilidade",
    "👨‍🦯‍➡️": "homem bengala cego frente direita accessibilidade",
    "👩‍🦯": "mulher bengala cego accessibilidade cega deficiencia visual",
    "👩‍🦯‍➡️": "mulher bengala cego frente direita accessibilidade cega deficiencia visual",
    "🧑‍🦼": "pessoa cadeira rodas motorizada acessibilidade",
    "🧑‍🦼‍➡️": "pessoa cadeira rodas motorizada frente direita acessibilidade",
    "👨‍🦼": "homem cadeira rodas motorizada acessibilidade",
    "👨‍🦼‍➡️": "homem cadeira rodas motorizada frente direita acessibilidade",
    "👩‍🦼": "mulher cadeira rodas motorizada acessibilidade",
    "👩‍🦼‍➡️": "mulher cadeira rodas motorizada frente direita acessibilidade",
    "🧑‍🦽": "pessoa cadeira rodas acessibilidade",
    "🧑‍🦽‍➡️": "pessoa cadeira rodas frente direita acessibilidade",
    "👨‍🦽": "homem cadeira rodas acessibilidade",
    "👨‍🦽‍➡️": "homem cadeira rodas frente direita acessibilidade",
    "👩‍🦽": "mulher cadeira rodas acessibilidade",
    "👩‍🦽‍➡️": "mulher cadeira rodas frente direita acessibilidade",
    "🏃": "pessoa correndo correr corrida esporte maratona maratonista pressa",
    "🏃‍♂️": "homem correndo corrida esporte maratona",
    "🏃‍♀️": "mulher correndo corredora corrida esporte garota guria maratona menina pressa rapida rapido",
    "🏃‍➡️": "pessoa correndo frente direita correr corrida esporte maratona maratonista pressa",
    "🏃‍♀️‍➡️": "mulher correndo frente direita corredora corrida esporte garota guria maratona menina pressa rapida rapido",
    "🏃‍♂️‍➡️": "homem correndo frente direita corrida esporte maratona",
    "🧑‍🩰": "bailarina bailarino bale dancarina dancarino",
    "💃": "mulher dancando bailarina danca flamenco pessoas salsa tango vamos dancar",
    "🕺": "homem dancando",
    "🕴️": "homem terno levitando",
    "👯": "pessoas orelhas coelho amigas dancar dancarinas festa gemeas melhores",
    "👯‍♂️": "homens orelhas coelho dancarino festa homem meninos",
    "👯‍♀️": "mulheres orelhas coelho dancarinas festa orelha",
    "🧖": "pessoa na sauna banho homem mulher relaxar spa",
    "🧖‍♂️": "homem na sauna",
    "🧖‍♀️": "mulher na sauna",
    "🧗": "pessoa escalando alpinista escalada escalar esporte montanha",
    "🧗‍♂️": "homem escalando escalar",
    "🧗‍♀️": "mulher escalando escalar",
    "🤺": "esgrimista esgrima espada esporte pessoa",
    "🏇": "corrida cavalos cavalo esporte jockey joquei",
    "⛷️": "esquiador esporte esqui frio neve",
    "🏂": "praticante snowboard esporte inverno neve ski",
    "🏌️": "golfista bola esporte golf golfe jogando",
    "🏌️‍♂️": "homem golfista golfe jogando",
    "🏌️‍♀️": "mulher golfista bola esporte garota golf golfe menina",
    "🏄": "surfista esporte onda ondas pessoa praia prancha surf",
    "🏄‍♂️": "homem surfista surfando",
    "🏄‍♀️": "mulher surfista esporte garota guria mar menina onda praia surfando surfe",
    "🚣": "pessoa remando barco bote canoa mar oceano remo rio",
    "🚣‍♂️": "homem remando esporte remador remo",
    "🚣‍♀️": "mulher remando barco caiaque canoa esporte garota menina pescaria remadora remo rio",
    "🏊": "pessoa nadando esporte nadar natacao olimpiada triatlo",
    "🏊‍♂️": "homem nadando nadar natacao",
    "🏊‍♀️": "mulher nadando esporte homem nadadora nado natacao olimpiada triatlo",
    "⛹️": "pessoa jogando basquete arremesso atleta bater bola cesta esporte jogador lance livre",
    "⛹️‍♂️": "homem jogando basquete bola esporte",
    "⛹️‍♀️": "mulher jogando basquete basketball bola esporte garota jogadora jogo menina",
    "🏋️": "pessoa levantando peso bodybuilder esporte fisiculturista forca levantar",
    "🏋️‍♂️": "homem levantando peso esporte forca",
    "🏋️‍♀️": "mulher levantando peso esporte forca garota guria levantadora levantar malhar menina",
    "🚴": "ciclista bicicleta bike ciclismo esporte pedalar",
    "🚴‍♂️": "homem ciclista bicicleta esporte passeio",
    "🚴‍♀️": "mulher ciclista bicicleta bike ciclismo esporte garota guria menina passeio pedalando pedalar",
    "🚵": "pessoa fazendo bike bicicleta ciclismo ciclista esporte montanha pedalando trilha",
    "🚵‍♂️": "homem fazendo bike bicicleta ciclista",
    "🚵‍♀️": "mulher fazendo bike bicicleta ciclista esporte garota guria menina montanha pedalar",
    "🤸": "pessoa fazendo estrela animada animado esporte estrelinha feliz ginasta ginastica",
    "🤸‍♂️": "homem fazendo estrela animado esporte estrelinha feliz ginastica menino virar",
    "🤸‍♀️": "mulher fazendo estrela animada esporte feliz ginasta ginastica menina virar",
    "🤼": "pessoas lutando combate esporte greco-romana livre luta lutador pessoa torneio",
    "🤼‍♂️": "homens lutando combate esporte greco-romana homem livre luta lutador torneio",
    "🤼‍♀️": "mulheres lutando combate esporte greco-romana livre luta lutadora mulher torneio",
    "🤽": "pessoa jogando aquatico competicao esporte piscina",
    "🤽‍♂️": "homem jogando aquatico esporte jogador piscina",
    "🤽‍♀️": "mulher jogando aquatico esporte jogadora menina piscina",
    "🤾": "handebol atleta bola esporte gol jogador passe pessoa",
    "🤾‍♂️": "jogador handebol bola esporte homem jogando menino quadra",
    "🤾‍♀️": "jogadora handebol bola esporte gol jogando menina mulher passe quadra",
    "🤹": "malabarista equilibrio habilidade malabares malabarismo multitarefa",
    "🤹‍♂️": "homem malabarista equilibrio habilidade malabares malabarismo multitarefa",
    "🤹‍♀️": "mulher malabarista equilibrio habilidade malabarismo multitarefa",
    "🧘": "pessoa na posicao cruzadas ioga meditacao pernas postura relaxar serenidade yoga zen",
    "🧘‍♂️": "homem na posicao ioga meditacao yoga",
    "🧘‍♀️": "mulher na posicao ioga meditacao yoga",
    "🛀": "pessoa tomando banho agua banheira espuma",
    "🛌": "pessoa deitada na cama boa cochilo dormindo dormir hotel noite soneca sono",
    "🧑‍🤝‍🧑": "pessoas maos dadas amigos casal gemeos parceiros parceria pessoa",
    "👭": "duas mulheres maos dadas amigas casal gemeas parceiras parceria pessoa",
    "👫": "homem mulher maos dadas amigos casal pessoas",
    "👬": "dois homens maos dadas amigos casal gemeos homem irmaos namorados pessoas",
    "💏": "beijo amor beijar casal coracao pessoas romance mulher homem",
    "💑": "casal apaixonado coracao pessoas romance mulher homem",
    "👨‍👩‍👦": "familia homem mulher menino filho mae pai pessoas menina adulto crianca adultos criancas filha",
    "🗣️": "silhueta falando berro cabeca grito voz",
    "👤": "silhueta busto misterio pessoas sombra",
    "👥": "silhueta bustos amigos busto dupla pessoas",
    "🫂": "pessoas abracando abraco adeus amizade amor carinho conforto obrigado ola",
    "👣": "pegadas corpo gravura pe pes descalcos rastro",
    "🫆": "impressao digital identidade investigacao forense seguranca",
    "🐵": "rosto macaco animal",
    "🐒": "macaco animal",
    "🦍": "gorila animal macaco",
    "🦧": "orangotango macaco primata",
    "🐶": "rosto cachorro animal",
    "🐕": "cachorro animal cao",
    "🦮": "cao-guia acessibilidade cachorro cadela cao cega cego cegueira deficiencia visual guia labrador",
    "🐕‍🦺": "cao servico accessibilidade assistencia cachorro cadela cao-guia pastor alemao",
    "🐩": "animal cachorro",
    "🐺": "rosto lobo animal",
    "🦊": "rosto raposa animal bicho",
    "🦝": "guaxinim animal astuto curioso travesso",
    "🐱": "rosto gato animal felino gatinho",
    "🐈": "gato animal felino",
    "🐈‍⬛": "gato preto animal azar dia bruxas felino gata halloween miado miau",
    "🦁": "rosto leao animal zodiaco",
    "🐯": "rosto tigre animal",
    "🐅": "tigre animal",
    "🐆": "leopardo animal onca",
    "🐴": "rosto cavalo animal equino",
    "🫎": "alce animal cervo chifres galhada mamifero rena",
    "🫏": "burro animais animal asno jegue mamifero mula relinchar relincho teimosa teimoso",
    "🐎": "cavalo animal corrida equestre",
    "🦄": "rosto unicornio animal",
    "🦓": "animal listra listras",
    "🦌": "cervo animal veado",
    "🦬": "bisao animal bisonte bufalo rebanho",
    "🐮": "rosto vaca animal fazenda leite muuu vaquinha",
    "🐂": "boi animal touro zodiaco",
    "🐃": "bufalo-asiatico agua animal bufalo",
    "🐄": "vaca animal fazenda",
    "🐷": "rosto porco animal",
    "🐖": "porco animal",
    "🐗": "javali animal",
    "🐽": "nariz porco animal rosto",
    "🐏": "carneiro animal aries chifre zodiaco",
    "🐑": "ovelha animal fazenda",
    "🐐": "cabra animal bode capricornio zodiaco",
    "🐪": "camelo animal so corcova deserto dromedario",
    "🐫": "camelo duas corcovas animal asiatico corcova deserto",
    "🦙": "lhama alpaca animal guanaco vicunha",
    "🦒": "girafa animal",
    "🐘": "elefante animal",
    "🦣": "mamute animal extinto grande lanoso pre-historico presa",
    "🦏": "rinoceronte animal",
    "🦛": "hipopotamo animal",
    "🐭": "rosto camundongo animal ratinho rato",
    "🐁": "camundongo animal ratinho",
    "🐀": "rato animal ratazana",
    "🐹": "rosto animal",
    "🐰": "rosto coelho animal",
    "🐇": "coelho animal coelhinho pascoa",
    "🐿️": "esquilo animal",
    "🦫": "castor animal dentuco represa",
    "🦔": "porco-espinho animal espinhoso ourico",
    "🦇": "morcego animal vampiro",
    "🐻": "rosto urso animal",
    "🐻‍❄️": "urso polar artico branco",
    "🐨": "coala animal",
    "🐼": "rosto animal",
    "🦥": "preguica bicho-preguica devagar lenta lentidao preguicosa preguicoso",
    "🦦": "lontra brincalhona pesca",
    "🦨": "gamba fedida fedido fedor",
    "🦘": "canguru animal australia filhote joey marsupial pula pulo salto",
    "🦡": "texugo animal incomodar ratel mel",
    "🐾": "patas animal pata patinhas cachorro pegada",
    "🦃": "peru animal ave natal",
    "🐔": "galinha animal ave",
    "🐓": "galo animal ave",
    "🐣": "pintinho chocando animal ave bebe filhote galinha pinto",
    "🐤": "pintinho perfil animal ave bebe filhote galinha pinto rosto",
    "🐥": "pintinho frente animal ave bebe fihote galinha olhando pinto",
    "🐦": "passaro animal",
    "🐧": "pinguim animal antartica frio",
    "🕊️": "pomba branca animal ave passaro paz",
    "🦅": "aguia passaro",
    "🦆": "pato animal passaro",
    "🦢": "cisne animal ave passaro patinho feio",
    "🦉": "coruja passaro sabedoria",
    "🦤": "animal ave extinto grande mauricio passaro",
    "🪶": "pena leve passaro plumagem voo",
    "🦩": "ave extravagante tropical",
    "🦚": "pavao animal ave colorido orgulhoso pavoa pomposo",
    "🦜": "papagaio animal ave fala passaro pirata repete",
    "🪽": "asa angelical anjo ascender aviacao celestial mitologia passaro voando voar alto",
    "🐦‍⬛": "passaro preto animal ave bico corvo gralha passarinho",
    "🪿": "ganso animal ave bobo gansos grasno marreco passaro pato",
    "🐦‍🔥": "fenix fantasia imortal passaro fogo reemergir reencarnacao reencarnar reincarnacao renascer renascimento reviver transformacao",
    "🐸": "sapo animal rosto",
    "🐊": "crocodilo animal jacare",
    "🐢": "tartaruga animal",
    "🦎": "lagartixa animal lagarto reptil",
    "🐍": "cobra animal reptil serpente",
    "🐲": "rosto dragao animal",
    "🐉": "dragao animal game thrones",
    "🦕": "sauropode braquiosaurus braquiossauro brontossauro brontossuro dinossauro diplodoco diplodocus",
    "🦖": "tiranossauro rex dinossauro",
    "🐳": "baleia esguichando agua animal esguicho",
    "🐋": "baleia animal mar oceano",
    "🐬": "golfinho animal oceano",
    "🫍": "orca baleia marinho oceano",
    "🦭": "foca animal marinho leao leao-marinho oceano",
    "🐟": "peixe animal peixes pesca pescar signo zodiaco",
    "🐠": "peixe animal",
    "🐡": "baiacu animal peixe",
    "🦈": "tubarao peixe",
    "🐙": "polvo animal oceano",
    "🐚": "caramujo animal concha mar espiral",
    "🪸": "mar mudanca climatica oceano recife",
    "🪼": "agua-viva ai animal aquario ferrao invertebrado mar marinha medusa oceano queimadura tentaculos",
    "🦀": "caranguejo animal marinho cancer signo zodiaco",
    "🦞": "lagosta animal bisque caldo fruto mar garras",
    "🦐": "camarao animal comida crustaceo",
    "🦑": "lula animal comida molusco",
    "🦪": "ostra frutos mar mergulho perola",
    "🐌": "caracol animal escargot",
    "🦋": "borboleta beleza inseto",
    "🐛": "inseto animal centopeia lagarta",
    "🐜": "formiga animal inseto",
    "🐝": "abelha animal inseto natureza",
    "🪲": "besouro animal bicho inseto",
    "🐞": "joaninha animal besouro inseto tipo",
    "🦗": "grilo gafanhoto inseto",
    "🪳": "barata animal baratas inseto nojento nojo praga",
    "🕷️": "aranha animal dias bruxas halloween inseto",
    "🕸️": "teia aranha halloween inseto",
    "🦂": "escorpiao animal deserto inseto zodiaco",
    "🦟": "aedes aegypti amarela animal chicungunha dengue doenca febre inseto malaria pernilongo virus zica",
    "🪰": "mosca animal apodrecendo doenca inseto larva mosca-varejeira praga varejeira",
    "🪱": "minhoca anelideo animal parasita verme",
    "🦠": "microbio ameba bacteria ciencia virus",
    "💐": "buque aniversario flor planta romance",
    "🌸": "flor cerejeira cereja planta primavera",
    "💮": "flor branca carimbo parabens",
    "🪷": "beleza budismo calma flor hinduismo india paz pureza serenidade vietna",
    "🏵️": "roseta flor amarela planta primavera",
    "🌹": "rosa flor",
    "🥀": "flor murcha morrendo murchando",
    "🌺": "hibisco flor planta primavera",
    "🌻": "girassol flor planta",
    "🌼": "flor florescer planta",
    "🌷": "tulipa flor",
    "🪻": "jacinto arbusto bluebonnet flor flor-cranio-do-dragao lavanda lilas lupinus planta primavera roxo violeta",
    "🌱": "muda planta brotar broto jovem plantinha",
    "🪴": "vaso planta casa chato decoracao nutrir plantar samambaia sem utilidade",
    "🌲": "conifera arvore pinheiro floresta natal pinheirinho",
    "🌳": "arvore caidica cheia desfolha natureza",
    "🌴": "palmeira arvore coqueiro",
    "🌵": "cacto deserto natureza planta seca",
    "🌾": "planta arroz comida espiga fazenda graos",
    "🌿": "erva folha planta",
    "☘️": "trevo irlanda irlandes planta tres folhas",
    "🍀": "trevo quatro folhas irlandes planta sorte sortudo",
    "🍁": "folha bordo caida vermelha outono",
    "🍂": "folhas caidas cair folha outono",
    "🍃": "folha ao vento soprando",
    "🪹": "ninho vazio aninhando galhos lar passarinho passaro",
    "🪺": "ninho ovos aninhando galhos lar ovo passarinho passaro",
    "🍄": "cogumelo champignon fungo mario planta",
    "🪾": "arvore seca sem folhas inverno",
    "🍇": "uvas fruta uva",
    "🍈": "melao fruta",
    "🍉": "melancia fruta",
    "🍊": "tangerina citrica fruta laranja",
    "🍋": "limao azedo citrico fruta lima siciliano",
    "🍋‍🟩": "lima acidez caipirinha citrica citrico drink fruta limao limonada tequila tropical verde",
    "🍌": "fruta",
    "🍍": "abacaxi comida fome fruta",
    "🥭": "manga comida fruta tropical",
    "🍎": "maca vermelha fruta",
    "🍏": "maca verde comida fome fruta",
    "🍐": "pera comida fome fruta",
    "🍑": "pessego fruta",
    "🍒": "cereja comida fome fruta frutas vermelhas",
    "🍓": "morango comida fome fruta frutas vermelhas",
    "🫐": "mirtilos alimento azul baga blueberry comida fruta mirtilo silvestre",
    "🥝": "kiwi comida fruta",
    "🍅": "tomate fruta fruto legume vegetal",
    "🫒": "azeitona alimento comida oliveira",
    "🥥": "coco fruta palmeira pina colada",
    "🥑": "abacate comida fruta",
    "🍆": "berinjela beringela comida legume vegetal",
    "🥔": "batata comida fome legume vegetal",
    "🥕": "cenoura comida fome legume salada vegetal",
    "🌽": "milho comida espiga fome",
    "🌶️": "pimenta apimentada apimentado tempero",
    "🫑": "pimentao alimento capsicum comida pimenta verde vegetal",
    "🥒": "pepino comida fome legume picles salada vegetal",
    "🥬": "verdura alface bok choy comida couve hamburguer repolho chines salada",
    "🥦": "brocolis brocoli",
    "🧄": "alho tempero",
    "🧅": "cebola tempero",
    "🥜": "amendoim comida",
    "🫘": "feijoes alimento comida feijao grao legume",
    "🌰": "castanha planta",
    "🫚": "gengibre cerveja comida erva especiaria natural raiz saude tempero",
    "🫛": "vagem comida edamame ervilha feijao grao legume leguminosa pe vegetal",
    "🍄‍🟫": "cogumelo marrom champignon comida fungo fungos natureza trufa vegetal vegetariano",
    "🫜": "tuberculo beterraba legume nabo raiz vegetal",
    "🍞": "pao forma fatiar restaurante torrada tostex",
    "🥐": "cafe manha comida fome pao",
    "🥖": "baguete bisnaga comida pao frances",
    "🫓": "pao sirio alimento arepa comida lavash naan folha pita",
    "🥨": "pao torcido",
    "🥯": "cafe manha chimia comida confeitaria pao rosca rosquinha",
    "🥞": "panquecas cafe manha comida crepes fome",
    "🧇": "doce gofre wafel wafle",
    "🧀": "queijo comida fome suico",
    "🍖": "carne osso no churras churrasco restaurante",
    "🍗": "coxa frango aves comida coxinha fome osso peru restaurante",
    "🥩": "corte carne bife vermelha costela cordeiro costeleta porco",
    "🥓": "comida fome toucinho",
    "🍔": "hamburguer burguer cheese restaurante",
    "🍟": "batata frita comida fast food fome fritas lanchonete restaurante",
    "🍕": "fatia restaurante",
    "🌭": "cachorro-quente cachorro quente pao salsicha vina",
    "🥪": "sanduiche pao forma",
    "🌮": "comida mexicana mexicano tacos",
    "🌯": "comida mexicana mexicano",
    "🫔": "alimento comida enrolado mexicano milho pamonha",
    "🥙": "pao recheado comida kebab recheio wrap",
    "🧆": "almondega grao bico",
    "🥚": "ovo comida fome",
    "🍳": "ovo frito comida culinaria frigideira estrelado restaurante",
    "🥘": "cacarola comida fome paella panela rasa",
    "🍲": "panela ensopado restaurante tigela comida",
    "🫕": "alimento chocolate comida derretido panela queijo suico",
    "🥣": "tigela colher cafe manha cereal prato sopa",
    "🥗": "salada verde comida fome restaurante",
    "🍿": "pipoca balde cinema",
    "🧈": "manteiga laticinio margarina",
    "🧂": "sal comida condimento sabor saleiro salgado",
    "🥫": "comida enlatada lata",
    "🍱": "almoco japones caixa restaurante",
    "🍘": "biscoito arroz bolacha",
    "🍙": "arroz japones bolinho comida fome onigiri restaurante",
    "🍚": "arroz cozido comida fome restaurante",
    "🍛": "arroz restaurante",
    "🍜": "lamen comida fome macarrao miojo quentinha ramen restaurante sopa tigela",
    "🍝": "espaguete comida fome italiano macarrao macarronada restaurante",
    "🍠": "batata assada batata-doce doce restaurante",
    "🍢": "espetinho frutos mar no restaurante",
    "🍣": "comida japonesa restaurante japones sashimi",
    "🍤": "camarao frito empanado restaurante tempura",
    "🍥": "bolinho peixe croquete redemoinho restaurante",
    "🥮": "bolo lunar lua comida confeitaria festival chines outono yuebing",
    "🍡": "bolinho mochi no espetinho japones comida doce fome mochiko sobremesa",
    "🥟": "bolinho asiatico chines empanada gyoza jiaozi massa pierogi potsticker",
    "🥠": "biscoito sorte chines fortuna profecia",
    "🥡": "caixa viagem caixinha levar delivery marmita",
    "🍦": "sorvete italiano doce fome sobremesa casquinha massa na",
    "🍧": "raspadinha gelo restaurante sobremesa",
    "🍨": "sorvete creme gelo restaurante sobremesa",
    "🍩": "donut comida fome restaurante rosquinha sonho doce",
    "🍪": "biscoito bolacha comida doce fome sobremesa",
    "🎂": "bolo aniversario comemoracao doce feliz festa niver",
    "🍰": "pao lo morango bolo recheado doce fatia restaurante sobremesa",
    "🧁": "acucar bolinho bolo comida confeitaria confeito doce sobremesa torta",
    "🥧": "torta abobora carne doce fatia fruta maca padaria recheio salgado",
    "🍫": "barra doce fome",
    "🍬": "bala balinha doce",
    "🍭": "pirulito bala doce",
    "🍮": "pudim leite ovos restaurante",
    "🍯": "pote mel restaurante",
    "🍼": "mamadeira bebe leite leitinho mamar nenem",
    "🥛": "copo leite",
    "☕": "cafe cafeina cafezinho cha inverno quente",
    "🫖": "bule alimento bebida cha chaleira comida infusao",
    "🍵": "xicara cha sem alca bebida verde",
    "🍶": "saque bar bebida copo garrafa japones restaurante",
    "🍾": "garrafa champanhe aniversario bar champagne champanha cidra comemorar espumante estourar festa parabens",
    "🍷": "vinho bar bebida calice restaurante taca",
    "🍸": "coquetel bar beber bebida cachaca happy hour martini restaurante taca",
    "🍹": "bebida bar coquetel frutas happy hour restaurante",
    "🍺": "cerveja bar bebida caneca chopp gelada happy hour restaurante",
    "🍻": "canecas cerveja bar caneca chope chopp restaurante",
    "🥂": "tacas brindando brinde champagne champanhe comemoracao taca tim-tim",
    "🥃": "copo bar bebida drink whisky",
    "🫗": "derramando liquido acabou acidente agua bebida copo derramar derrubar fim ops pingar vazio",
    "🥤": "copo canudo agua refrigerante suco",
    "🧋": "cha perolado bebida bolha comida leite perola poba",
    "🧃": "suco caixa caixinha suquinho",
    "🧉": "bebida chimarrao cuia terere",
    "🧊": "cubo gelo gelado iceberg",
    "🥢": "hashi comida japonesa pauzinhos",
    "🍽️": "prato talheres almoco faca fome garfo jantar ao lado restaurante",
    "🍴": "garfo faca almoco comer comida fome jantar restaurante talher talheres",
    "🥄": "colher talher",
    "🔪": "faca cozinha chef cozinhar",
    "🫙": "jarro armazenar condimento conserva jarra molho nada pote recipiente vazio vidro",
    "🏺": "anfora ornamento chines vaso",
    "🎃": "abobora halloween comemoracao dia bruxas jack lanterna",
    "🎄": "arvore natal comemoracao pinheirinho",
    "🎆": "fogos artificio ano novo comemoracao",
    "🎇": "vela estrela comemoracao fogo artificio",
    "🧨": "bombinha artificio bomba chiando dinamite estourar explosivo faisca fogo luz pirotecnia roubada",
    "✨": "brilhos * brilhantes estrelas faiscas magica",
    "🎈": "balao aniversario celebracao comemoracao festa parabens",
    "🎊": "confete bola celebracao comemoracao eba oba parabens",
    "🎋": "arvore comemoracao estrelas festival japonesa papel tiras",
    "🎍": "decoracao pinhos bambu comemoracao pinhas japones pinha",
    "🎎": "bonecas japonesas comemoracao festival japanesas japones",
    "🎏": "bandeira carpas comemoracao koinobori",
    "🎐": "carrilhao vento sino som",
    "🎑": "contemplacao lua cerimonia comemoracao",
    "🧧": "vermelho boa sorte dinheiro hongbao lai see presente",
    "🎀": "laco fita comemoracao presente",
    "🎁": "presente comemoracao embrulhado mimo",
    "🎗️": "fita lembrete celebracao comemoracao laco",
    "🎟️": "ingresso cinema entrada ticket",
    "🎫": "ingresso entretenimento",
    "🎖️": "medalha militar condecoracao premio",
    "🏆": "trofeu campea campeao premio",
    "🏅": "medalha esportiva vitoria",
    "🥇": "medalha ouro 1o lugar vitoria",
    "🥈": "medalha prata 2o lugar segundo",
    "🥉": "medalha bronze 3o lugar terceiro",
    "⚽": "bola futebol jogar",
    "⚾": "bola beisebol esportes",
    "🥎": "softbol bola esporte luva",
    "🏀": "bola basquete cesta esporte jogo",
    "🏐": "bola volei jogar jogo",
    "🏈": "bola futebol americano esporte super bowl",
    "🏉": "bola americano esporte futebol",
    "🎾": "tenis bola esporte raquete",
    "🥏": "frisbee disco esporte ultimate voador",
    "🎳": "boliche bola jogo strike",
    "🏏": "criquete betes bola jogo taco",
    "🏑": "hoquei campo bola jogo taco",
    "🏒": "hoquei no gelo disco jogo taco",
    "🥍": "bastao bola esporte gol raquete taco",
    "🏓": "pingue-pongue mesa pingpong raquete tenis",
    "🏸": "jogo peteca raquete",
    "🥊": "luva boxe esporte luta",
    "🥋": "quimono artes marciais esporte faixa judo karate preta taekwondo uniforme",
    "🥅": "gol esporte futebol goleira goleiro rede",
    "⛳": "bandeira no buraco esporte golfe",
    "⛸️": "patins gelo patinacao",
    "🎣": "pesca entretenimento peixe pescador pescaria recreacao vara",
    "🤿": "mascara mergulho esnorquel mergulhador scuba snorkel snorkeling",
    "🎽": "camiseta corrida esporte faixa",
    "🎿": "esqui bota esporte esquiar neve",
    "🛷": "treno neve toboga",
    "🥌": "pedra jogo",
    "🎯": "no alvo certeiro dardos jogo mira mosca tiro",
    "🪀": "ioio brinquedo flutua",
    "🪁": "pipa papagaio planar voar",
    "🔫": "pistola d’agua agua arma ferramenta revolver",
    "🎱": "bilhar bola oito jogo",
    "🔮": "bola cristal adivinhacao destino futuro prever",
    "🪄": "varinha magica bruxa condao magia mago",
    "🎮": "videogame controle jogo jogos playstation xbox",
    "🕹️": "atari controle game jogo video videogame",
    "🎰": "caca-niquel aposta azar cassino jogo maquina",
    "🎲": "jogo dado dados sorte",
    "🧩": "quebra-cabeca dica encaixe peca",
    "🧸": "ursinho pelucia bichinho brinquedo enchimento urso",
    "🪅": "pinhata cinco maio comemoracao doce festa festival",
    "🪩": "globo espelhos balada boate bola brilho danca dancar discoteca espelhado espelho festa night",
    "🪆": "boneca russa matriosca matrioska",
    "♠️": "naipe espadas baralho carta jogo",
    "♥️": "naipe copas baralho carta jogo s2",
    "♦️": "naipe ouros baralho carta jogo ouro",
    "♣️": "naipe paus baralho carta jogo",
    "♟️": "peao xadrez truque",
    "🃏": "curinga baralho carta coringa jogo",
    "🀄": "dragao vermelho jogo peca",
    "🎴": "carta flores baralho hanafuda jogo",
    "🎭": "mascara arte ator atriz drama dramatica entretenimento espetaculo peca performance teatro",
    "🖼️": "quadro emoldurado arte moldura museu pintura",
    "🎨": "paleta tintas arte artista artistica museu pintor pintura tinta",
    "🧵": "carretel agulha barbante costura linha",
    "🪡": "agulha costura alfaiataria bordados linha pontos suturas vagonite",
    "🧶": "novelo bola croche trico tricotar",
    "🪢": "no amarrar corda cordel emaranhado fio laco marinheiro",
    "🌍": "globo mostrando europa africa terra",
    "🌎": "globo mostrando america terra",
    "🌏": "globo mostrando asia oceania australia planeta terra",
    "🌐": "globo meridianos",
    "🗺️": "mapa-mundi geografia mapa mundo",
    "🗾": "mapa japao",
    "🧭": "bussola magnetica direcao ima magnetico navegacao norte orientacao pontos cardeais",
    "🏔️": "montanha neve alpes frio",
    "⛰️": "montanha natureza",
    "🛘": "deslizamento avalanche desastre montanha perigo rochas terremoto",
    "🌋": "vulcao erupcao vulcanica lava natureza",
    "🗻": "monte montanha",
    "🏕️": "acampamento barraca",
    "🏖️": "praia guarda-sol",
    "🏜️": "deserto clima seco",
    "🏝️": "ilha deserta palmeira praia",
    "🏞️": "parque nacional rio reserva florestal",
    "🏟️": "estadio arena",
    "🏛️": "predio grego arquitetura classico",
    "🏗️": "construcao guindaste obra",
    "🧱": "tijolo argamassa argila cimento muro parede terra",
    "🪨": "pedra pedregulho pesado rocha seixo solido",
    "🪵": "madeira lenha serrada tora tronco",
    "🛖": "cabana abrigo casa yurt",
    "🏘️": "casas",
    "🏚️": "casa abandonada",
    "🏠": "casa construcao domicilio lar residencia",
    "🏡": "casa jardim construcao lar",
    "🏢": "edificio comercial escritorio",
    "🏣": "correio japones oriental predio",
    "🏤": "correio europeu predio",
    "🏥": "doente internado medico predio",
    "🏦": "banco predio",
    "🏨": "predio",
    "🏩": "motel amor predio",
    "🏪": "loja conveniencia 24 horas 24h predio",
    "🏫": "escola colegio predio",
    "🏬": "loja departamentos estabelecimento comercial",
    "🏭": "fabrica predio",
    "🏯": "castelo japones pagoda pagode predio",
    "🏰": "castelo europeu medieval predio",
    "💒": "capela casamento romance",
    "🗼": "torre toquio japao",
    "🗽": "estatua liberdade",
    "⛪": "igreja capela crista cristao missa predio religiao",
    "🕌": "mesquita isla muculmano religiao",
    "🛕": "templo",
    "🕍": "sinagoga judaismo judeu judia religiao templo",
    "⛩️": "santuario japones oriental religiao xintoismo",
    "🕋": "caaba isla muculmano religiao",
    "⛲": "fonte agua chafariz praca",
    "⛺": "barraca acampamento acampar",
    "🌁": "enevoado bruma cerracao neblina nevoa nevoeiro",
    "🌃": "noite estrelada cidade estrelas predios",
    "🏙️": "cidade predios urbano",
    "🌄": "aurora sobre montanhas montanha nascer sol manha",
    "🌅": "aurora sobre agua amanhecer natureza oceano rio sol manha nascendo",
    "🌆": "cidade ao anoitecer noite paisagem sol predio",
    "🌇": "sol anoitecer calor cidade entardecer sobre predios predio",
    "🌉": "ponte noite pontilhao",
    "♨️": "chamas calor fogo fumaca quente",
    "🎠": "carrossel cavalo entretenimento parque diversao",
    "🛝": "escorregador brincar brinquedo escorregar parque diversoes parquinho",
    "🎡": "roda gigante entretenimento parque diversoes",
    "🎢": "montanha russa entretenimento parque diversoes tematico",
    "💈": "barbearia barbeiro cortar cabelo poste",
    "🎪": "circo entretenimento lona tenda",
    "🚂": "locomotiva trem vapor veiculo",
    "🚃": "vagao trem bonde eletrico ferroviario transporte trolebus veiculo",
    "🚄": "trem alta velocidade bala japones trens veiculo veloz",
    "🚅": "trem alta velocidade japones bala ferrovia japao veloz viagem",
    "🚆": "trem ferrovia veiculo",
    "🚇": "trem tunel subterraneo veiculo",
    "🚈": "trem urbano chegada cheguei leve veiculo",
    "🚉": "estacao trem metro",
    "🚊": "bonde eletrico trolebus veiculo",
    "🚝": "monotrilho trem veiculo",
    "🚞": "estrada ferro na montanha carro teleferico veiculo",
    "🚋": "bonde eletrico carro trolebus veiculo",
    "🚌": "onibus transporte publico veiculo",
    "🚍": "onibus aproximando veiculo",
    "🚎": "trolebus bonde eletrico onibus movido eletricidade veiculo",
    "🚐": "van mini onibus veiculo veraneio",
    "🚑": "ambulancia veiculo",
    "🚒": "carro corpo bombeiros bombeiro caminhao fogo incendio veiculo",
    "🚓": "viatura policial carro patrulha policia veiculo",
    "🚔": "viatura policial aproximando policia veiculo",
    "🚕": "veiculo",
    "🚖": "aproximando chegada transporte veiculo",
    "🚗": "carro automovel veiculo",
    "🚘": "carro aproximando automovel carona chegada veiculo",
    "🚙": "suv carro trailer veiculo recreacional",
    "🛻": "caminhonete automovel cacamba caminhao carro picape pick-up transporte veiculo",
    "🚚": "caminhao entrega veiculo",
    "🚛": "caminhao articulado semi trailer veiculo",
    "🚜": "trator obra veiculo",
    "🏎️": "carro corrida automobilismo",
    "🏍️": "motocicleta corrida moto veiculo",
    "🛵": "monareta motinho moto motoca",
    "🦽": "cadeira rodas acessibilidade",
    "🦼": "cadeira rodas motorizada acessibilidade",
    "🛺": "automovel riquixa autorriquixa tuk",
    "🚲": "bicicleta bike veiculo",
    "🛴": "patinete brinquedo",
    "🛹": "skate rodinhas skatista",
    "🛼": "patins rodas esporte patinacao",
    "🚏": "ponto onibus busao transporte publico",
    "🛣️": "estrada caminho viagem viajar",
    "🛤️": "trilhos trem",
    "🛢️": "barril oleo lata latao",
    "⛽": "posto gasolina abastecer alcool bomba combustivel",
    "🛞": "roda carro circulo direcao girar pneu veiculo volante",
    "🚨": "sirene carro policia farol policial luz viatura giratoria",
    "🚥": "semaforo cruzamento luz sinal sinaleira transito",
    "🚦": "semaforo cruzamento luz sinal sinaleira transito",
    "🛑": "sinal pare",
    "🚧": "construcao simbolo “em construcao”",
    "⚓": "ancora marinha navegar sinal",
    "🛟": "boia colete flutuar guarda-vidas guardar nadar resgate salva salva-vidas seguranca vidas",
    "⛵": "barco vela iate navegar oceano praia resort",
    "🛶": "canoa barco",
    "🚤": "lancha barco ferias praia veiculo verao",
    "🛳️": "cruzeiro barco embarcacao navio passageiros veiculo",
    "⛴️": "balsa barco boat ferry-boat navegar",
    "🛥️": "barco lancha navio veiculo",
    "🚢": "navio aquatico barco cruzeiro ferias veiculo viagem",
    "✈️": "aviao aereo veiculo viajar voar voo",
    "🛩️": "aviao pequeno aereo jatinho jato veiculo",
    "🛫": "aviao decolando decolar ferias fui partindo partiu viagem viajando",
    "🛬": "aviao aterrissando aterrissagem chegando pousando veiculo voltando",
    "🪂": "paraquedas asa-delta paraquedista parasail saltar",
    "💺": "assento cadeira poltrona",
    "🚁": "helicoptero veiculo viagem viajar voo",
    "🚟": "estrada ferro suspensa suspensao trem veiculo",
    "🚠": "teleferico montanha bonde cabo suspenso usado telefericos nas montanhas na veiculo",
    "🚡": "teleferico aerea bonde gondola linha veiculo",
    "🛰️": "satelite antena espaco",
    "🚀": "foguete espaco veiculo",
    "🛸": "disco voador alienigena et extra ovni terrestre",
    "🛎️": "sineta hotel portaria sino",
    "🧳": "mala bagagem rodinhas viagem",
    "⌛": "ampulheta areia tempo",
    "⏳": "ampulheta contando tempo areia cheia cima hora relogio",
    "⌚": "relogio pulso hora tempo",
    "⏰": "despertador alarme atrasada atrasado hora horario relogio",
    "⏱️": "cronometro relogio tempo",
    "⏲️": "relogio temporizador cronometro",
    "🕰️": "relogio mesa antigo",
    "🕛": "12 horas 12h00 doze meia-noite meio-dia relogio",
    "🕧": "doze meia 12h30 relogio",
    "🕐": "1 hora 13h 1h 1h00 relogio",
    "🕜": "meia 1:30 13:30 13h30 1h30 relogio",
    "🕑": "2 horas 14h 2h 2h00 duas relogio",
    "🕝": "duas meia 2:30 2h30 relogio",
    "🕒": "3 horas 15h 3:00 3h00 hora horario relogio tres",
    "🕞": "tres meia 3:30 3h30 relogio",
    "🕓": "4 horas 4:00 4h00 horario quatro relogio",
    "🕟": "quatro meia 4h30 relogio",
    "🕔": "5 horas 5h00 cinco relogio",
    "🕠": "cinco meia 17h30 5h30 relogio",
    "🕕": "6 horas 18h 6h00 relogio seis",
    "🕡": "seis meia 18h30 6h30 relogio",
    "🕖": "7 horas 19h 7h00 relogio sete",
    "🕢": "sete meia 7:30 7h30 relogio",
    "🕗": "8 horas 8:00 8h00 hora oito relogio",
    "🕣": "oito meia 8:30 8h30 relogio",
    "🕘": "9 horas 21h 9h00 nove relogio",
    "🕤": "nove meia 9:30 9h30 hora relogio",
    "🕙": "10 horas 10h00 22h dez relogio",
    "🕥": "dez meia 10h30 22h30 relogio",
    "🕚": "11 horas 11:00 11h00 onze relogio",
    "🕦": "onze meia 11h30 23h30 relogio",
    "🌑": "lua nova escuro negra noite",
    "🌒": "lua crescente concava noite",
    "🌓": "quarto crescente lua",
    "🌔": "lua crescente convexa",
    "🌕": "lua cheia luar",
    "🌖": "lua minguante convexa",
    "🌗": "quarto minguante lua",
    "🌘": "lua minguante concava",
    "🌙": "lua crescente",
    "🌚": "rosto lua nova",
    "🌛": "rosto lua quarto crescente noite",
    "🌜": "rosto lua quarto minguante",
    "🌡️": "termometro clima temperatura tempo",
    "☀️": "sol clima dia claro raios solar tempo",
    "🌝": "rosto lua cheia",
    "🌞": "rosto sol praia",
    "🪐": "planeta aneis saturnino saturno",
    "⭐": "estrela branca media amarela astronomia",
    "🌟": "estrela brilhante brilho cintilante luminosa reluzente",
    "🌠": "estrela cadente cai",
    "🌌": "via lactea ceu espaco estrelado",
    "☁️": "nuvem clima",
    "⛅": "sol tras nuvens clima nublado nuvem",
    "⛈️": "chuva trovao clima nuvem relampago temporal",
    "🌤️": "sol nuvens clima ensolarado nublado nuvem",
    "🌥️": "nublado clima nuvem sol",
    "🌦️": "sol chuva clima nuvem",
    "🌧️": "nuvem chuva chovendo clima",
    "🌨️": "nuvem neve clima frio",
    "🌩️": "nuvem trovao clima relampago",
    "🌪️": "clima furacao nuvem",
    "🌫️": "nevoeiro clima furacao neblina nuvem",
    "🌬️": "rosto vento clima nuvem soprar sopro",
    "🌀": "ciclone clima espiral furacao tonto twister zonzo",
    "🌈": "arco-iris chuva clima gay lesbica lgbt lgbtqia+ natureza orgulho queer transgenero",
    "🌂": "guarda-chuva fechado chuva chuvoso",
    "☂️": "guarda-chuva acessorio chuva clima sombrinha aberta",
    "☔": "sombrinha na chuva acessorio clima gotas guarda-chuva",
    "⛱️": "guarda-sol chuva clima praia sol sombra sombrinha",
    "⚡": "alta tensao eletricidade natureza perigo relampago sinal",
    "❄️": "floco neve clima frio",
    "☃️": "boneco neve clima frio inverno",
    "⛄": "boneco neve sem frio inverno",
    "☄️": "cometa espaco estrela cadente meteoro satelite",
    "💧": "gota suor engracado",
    "🌊": "onda agua mar oceano praia surfe tsunami",
    "👓": "oculos acessorio",
    "🕶️": "oculos escuros sol",
    "🥽": "oculos protecao mergulho natacao olhos ski solda soldagem",
    "🥼": "jaleco cientista dr experiencia experimento medico roupa",
    "🦺": "colete salva-vidas emergencia refletivo seguranca",
    "👔": "gravata acessorio roupa",
    "👕": "camiseta camisa roupa",
    "👖": "calca roupa",
    "🧣": "cachecol frio inverno pescoco",
    "🧤": "luvas inverno luva mao",
    "🧥": "casaco agasalho blusa frio jaqueta",
    "🧦": "meias meia meiao",
    "👗": "vestido peca unica roupa",
    "👘": "quimono roupa vestir",
    "🥻": "india indiana roupa vestido",
    "🩱": "maio banho nadar praia roupa",
    "🩲": "cueca banho intima nadar praia roupa sunga",
    "🩳": "banho bermuda nadar praia roupa",
    "👙": "biquini banho piscina praia roupa",
    "👚": "roupas femininas azul blusa camiseta feminina roupa",
    "🪭": "leque dobravel abanar arejar balancar calor danca flertar quente refrescar timidez",
    "👛": "bolsinha acessorio bolsa moeda niqueleira porta rosa",
    "👜": "bolsa mao acessorio fashion mala",
    "👝": "bolsa pequena acessorio carteira fashion",
    "🛍️": "sacolas compras presente",
    "🎒": "mochila bolsa escola viagem viajar",
    "🩴": "chinelo alpargatas piscina praia rasteirinha sandalia",
    "👞": "sapato masculino acessorio sapatos",
    "👟": "tenis corrida acessorio correr esportivo sapato",
    "🥾": "bota trekking acampamento acampar caminhada marrom mochilao natureza sapato trilha",
    "🥿": "sapatilha confortavel bale sapato sem fivela",
    "👠": "sapato salto alto acessorio chique fashion moda mulher scarpin",
    "👡": "sandalia feminina acessorio feminino medio salto",
    "🩰": "sapatilha bale bailarina danca",
    "👢": "bota feminina acessorio calcado medio salto",
    "🪮": "pente cabelo afro crespo garfo pentear retro",
    "👑": "coroa acessorio familia real game thrones medieval rainha realeza rei reinado",
    "👒": "chapeu feminino acessorio fashion",
    "🎩": "cartola chapeu chique entretenimento roupa",
    "🎓": "chapeu formatura capelo comemoracao",
    "🧢": "bone beisebol chapeu",
    "🪖": "capacete militar exercito guerra guerreiro soldado",
    "⛑️": "capacacete socorrista ajuda branca capacete cruz primeiros resgate rosto salvamento socorros vermelho",
    "📿": "rosario oracao acessorio religiao reza terco",
    "💄": "batom cosmeticos makeup maquiagem vermelho",
    "💍": "anel diamante romance",
    "💎": "pedra preciosa diamante joia",
    "🔇": "alto-falante silenciado calar mudo mute quieto silenciar silencio som",
    "🔈": "alto-falante volume baixo mudo musica silencio som",
    "🔉": "alto-falante volume medio baixo diminuir som",
    "🔊": "alto-falante volume alto gritar musica som",
    "📢": "buzina alto alto-falante aviso comunicado discurso gritar megafone",
    "📣": "megafone aplausos comunicacao",
    "📯": "corneta correios",
    "🔔": "sino capela",
    "🔕": "sino silenciado mudo notificacao proibido quieto sem som silencio silencioso",
    "🎼": "partitura musica",
    "🎵": "nota musica",
    "🎶": "notas musicais musica nota",
    "🎙️": "microfone estudio musica",
    "🎚️": "controle volume musica",
    "🎛️": "botoes giratorios controle musica",
    "🎤": "microfone cantar entretenimento karaoke mic musica",
    "🎧": "fones ouvido entretenimento fone musica som",
    "📻": "radiola som video",
    "🎷": "saxofone instrumento musical musica sax",
    "🎺": "trompete instrumento musical musica",
    "🪊": "trombone instrumento jazz musica sopro triste",
    "🪗": "acordeao concertina instrumento musica sanfona",
    "🎸": "guitarra instrumento musical musica",
    "🎹": "teclado instrumento musica piano",
    "🎻": "violino instrumento musical musica",
    "🪕": "cordas instrumento musica",
    "🥁": "tambor baquetas musica percussao",
    "🪘": "tambor comprido atabaque batida conga instrumento percussao ritmo",
    "🪇": "chacoalhar chocalho danca festa instrumento maraca mexer musica percussao",
    "🪈": "flauta banda flautim instrumento marcial musica orquestra pifano pife sopro tubo",
    "🪉": "harpa amor cupido instrumento musica orquestra",
    "📱": "telefone celular movel",
    "📲": "telefone celular seta chamada fazer ligar receber smartphone",
    "☎️": "telefone no gancho",
    "📞": "telefone aparelho comunicacao",
    "📟": "comunicacao",
    "📠": "comunicacao maquina",
    "🔋": "pilha bateria",
    "🪫": "pouca bateria acabando cansado descarregada eletronico energia fim fraca pilha sem",
    "🔌": "tomada eletrica cabo eletricidade energia plugue",
    "💻": "laptop computador notebook pc pessoal trabalho",
    "🖥️": "computador mesa monitor",
    "🖨️": "impressora acessorio computador documento impressao imprimir",
    "⌨️": "teclado acessorio computador digitacao",
    "🖱️": "acessorio computador",
    "🖲️": "acessorio bolinha computador mouse",
    "💽": "computacao disc disco hd md mini rigido",
    "💾": "disquete computador disco flexivel",
    "💿": "cd blu-ray computador disco dvd optico",
    "📀": "blu-ray cd computador disco optico",
    "🧮": "abaco calculadora calculo matematica numero",
    "🎥": "cinema entretenimento filmar filme hollywood",
    "🎞️": "rolo filmes cinema filme",
    "📽️": "projetor filmes cinema filme video",
    "🎬": "claquete cena entretenimento filme tomada",
    "📺": "televisao canal tv video",
    "📷": "foto selfie video",
    "📸": "foto fotografia video",
    "📹": "filmadora",
    "📼": "videocassete cassete fita vhs video",
    "🔍": "lupa esquerda aumento busca ferramenta lente pesquisa procura",
    "🔎": "lupa direita aumento busca ferramenta lente pesquisa procura",
    "🕯️": "vela acesa luz",
    "💡": "lampada eletrica ideia luz quadrinhos tenho",
    "🔦": "lanterna eletrica ferramenta luz",
    "🏮": "lanterna vermelha papel bar restaurante",
    "🪔": "lampada oleo",
    "📔": "caderno decorado agenda capa decoracao escola livro",
    "📕": "livro fechado apostila",
    "📖": "livro aberto biblioteca leitura lendo ler livraria",
    "📗": "livro verde biblioteca caderno colegio escola estudar lendo ler livraria",
    "📘": "livro azul apostila escola estudo leitura",
    "📙": "livro laranja apostila cartilha",
    "📚": "livros biblioteca estudando estudar leitura lendo ler livraria livro",
    "📓": "caderno folhas",
    "📒": "livro contabil caderno",
    "📃": "pagina dobrada dobrado documento papel",
    "📜": "pergaminho rolo papel",
    "📄": "pagina voltada cima documento oficial papel",
    "📰": "jornal noticias",
    "🗞️": "jornal enrolado noticias",
    "📑": "marcadores pagina marcador marcar",
    "🔖": "marcador pagina livro",
    "🏷️": "etiqueta identificar rotulo",
    "🪙": "moeda dinheiro dolar euro metal ouro prata real rica rico tesouro",
    "💰": "saco dinheiro dolares",
    "🪎": "bau tesouro dinheiro joia joias ouro prata premio riqueza",
    "💴": "nota iene cedula dinheiro grana moeda",
    "💵": "nota dolar dinheiro moeda bancaria",
    "💶": "nota cedula dinheiro grana moeda",
    "💷": "nota libra cedula dinheiro grana moeda",
    "💸": "dinheiro voando asas banco cedula indo embora nota",
    "💳": "cartao credito banco debito dinheiro",
    "🧾": "recibo contabilidade escrituracao evidencia fatura prova",
    "💹": "grafico subindo iene ascendente ascensao crescimento dinheiro alta mercado",
    "✉️": "carta correspondencia e-mail email",
    "📧": "carta comunicacao correspondencia",
    "📨": "chegando carta comunicacao correspondencia e-mail nova recebida",
    "📩": "seta carta comunicacao correspondencia e-mail email",
    "📤": "bandeja saida caixa comunicacao correspondencia enviada",
    "📥": "bandeja entrada caixa comunicacao correspondencia e-mail email recebida recebido",
    "📦": "pacote caixa embrulho",
    "📫": "caixa correio fechada bandeira levantada correspondencia",
    "📪": "caixa correio fechada bandeira abaixada correspondencia vazia",
    "📬": "caixa correio aberta bandeira levantada correspondencia",
    "📭": "caixa correio aberta bandeira abaixada correspondencia vazia",
    "📮": "caixa correio carta correspondencia enviar",
    "🗳️": "urna eleitoral cedula eleicao votar voto",
    "✏️": "lapis",
    "✒️": "ponta caneta tinteiro preto tinta",
    "🖋️": "caneta tinteiro destro",
    "🖊️": "caneta esferografica tinteiro",
    "🖌️": "pincel pintando pintar",
    "🖍️": "giz cera desenho vermelho",
    "📝": "memorando anotacoes caderno comunicacao lapis notas",
    "💼": "maleta mala pasta",
    "📁": "pasta arquivos arquivo",
    "📂": "pasta arquivos aberta abrir arquivo",
    "🗂️": "divisores pastas arquivo divisor indice organizador pasta",
    "📅": "calendario data datas",
    "📆": "calendario folhas destacaveis data destacavel dia folha paginas varias",
    "🗒️": "bloco espiral caderno",
    "🗓️": "calendario espiral bloco data",
    "📇": "indice cartoes",
    "📈": "grafico subindo crescimento diagrama tendencia",
    "📉": "grafico caindo diagrama tendencia negativa",
    "📊": "grafico barras barra diagrama",
    "📋": "prancheta anotacoes",
    "📌": "tacha alfinete",
    "📍": "tacha redonda alfinete localizacao mapa",
    "📎": "clipe papel",
    "🖇️": "clipes papel conectados clipe",
    "📏": "regua reta",
    "📐": "regua angulo geometria matematica triangulo",
    "✂️": "tesoura aberta cortar ferramenta",
    "🗃️": "caixa arquivos arquivo cartao documentos ficheiro",
    "🗄️": "gavetas escritorio arquivo gabinete",
    "🗑️": "lixeira cesto lixo",
    "🔒": "cadeado fechado trancado",
    "🔓": "cadeado aberto destrancado",
    "🔏": "cadeado caneta privacidade privado tinteiro",
    "🔐": "cadeado fechado chave seguro trancado",
    "🔑": "chave senha trancado trancar",
    "🗝️": "chave antiga fechadura",
    "🔨": "martelo construcao ferramenta martelada reparo",
    "🪓": "machado cortar madeira partir",
    "⛏️": "picareta ferramenta mineracao",
    "⚒️": "martelo picareta ferramenta geologia",
    "🛠️": "martelo chave-inglesa chave ferramenta inglesa",
    "🗡️": "adaga arma faca",
    "⚔️": "espadas cruzadas arma batalha guerra",
    "💣": "bomba emocao engracado explosao explosivo perigo",
    "🪃": "bumerangue aborigene arma australia rebote repercussao",
    "🏹": "arco flecha arma sagitario zodiaco",
    "🛡️": "escudo arma",
    "🪚": "serrote carpinteiro cortar ferramenta madeira serra serrar",
    "🔧": "chave inglesa ferramenta obra reforma",
    "🪛": "chave fenda ferramenta",
    "🔩": "porca parafuso construcao ferramenta",
    "⚙️": "engrenagem ferramenta",
    "🗜️": "bracadeira compressao ferramenta morsa torno",
    "⚖️": "balanca ferramenta justica libra peso zodiaco",
    "🦯": "bengala cegos acessibilidade cega cego cegueira deficiente visual",
    "🔗": "aneis corrente dois simbolo vinculo",
    "⛓️‍💥": "corrente quebrada algema liberdade quebrando quebrar",
    "⛓️": "correntes corrente metal",
    "🪝": "gancho atracao venda bandido curva pegar prender",
    "🧰": "caixa ferramentas ferramenta mecanico vermelha",
    "🧲": "ima atracao ferradura magnetico positivo-negativo",
    "🪜": "escada degrau mao subir",
    "🪏": "pa buraco cavar pazinha",
    "⚗️": "alambique ferramenta quimica",
    "🧪": "tubo ensaio ciencia experiencia experimento laboratorio quimica quimico",
    "🧫": "placa bacteria biologia biologista biologo ciencia cultura laboratorio",
    "🧬": "biologista biologo codigo evolucao gene genetica genetico vida",
    "🔬": "microscopio ciencia ferramenta microscopico",
    "🔭": "telescopio ciencia ferramenta",
    "📡": "antena parabolica comunicacao satelite",
    "💉": "seringa agulha injecao medico remedio vacina vacinacao",
    "🩸": "gota sangue doacao medicina menstruacao sangrar",
    "💊": "comprimido capsula medicina medico pilula remedio",
    "🩹": "atadura adesiva bandaid bandeide curativo",
    "🩼": "muleta ajuda bastao bengala deficiencia desculpa lesao machucar mobilidade",
    "🩺": "estetoscopio coracao medica medicina medico tum",
    "🩻": "raio x doutor esqueleto medico ossos radiografia",
    "🚪": "porta fechada",
    "🛗": "elevador acessibilidade descer elevar subir",
    "🪞": "espelho especulo maquiagem refletor reflexao reflexo",
    "🪟": "janela abertura ar fresco quadro transparente vidro vista",
    "🛏️": "cama dormir hotel sono",
    "🛋️": "sofa luminaria hotel lampada",
    "🪑": "cadeira assento sentar",
    "🚽": "vaso sanitario banheiro patente privada toalete",
    "🪠": "desentupidor banheiro coco encanador fezes sanitario succao vaso",
    "🚿": "chuveiro agua banho ducha",
    "🛁": "banheira banho",
    "🪤": "ratoeira armadilha atrair isca prender queijo",
    "🪒": "lamina afiada afiado barbeador barbear depilar gilete raspar",
    "🧴": "frasco locao condicionador creme hidratante protetor shampoo solar xampu",
    "🧷": "alfinete seguranca fralda punk rock",
    "🧹": "vassoura bruxa limpar limpeza varrer",
    "🧺": "cesta agricola agricultura lavanderia lavoura piquenique roupa suja",
    "🧻": "rolo papel higienico toalha",
    "🪣": "balde baldinho barril",
    "🧼": "sabonete banho barra espuma limpar limpeza saboneteira",
    "🫧": "bolhas agua aquatica aquatico arroto bolha embaixo d’agua flutuar limpo perolas sabao",
    "🪥": "escova dentes banheiro dental higiene limpeza",
    "🧽": "esponja absorcao absorvente encharcar limpeza porosa poroso",
    "🧯": "extintor incendio apagar extinguir fogo",
    "🛒": "carrinho compras mercado supermercado",
    "🚬": "cigarro permitido fumar fumante fumo simbolo “e fumar”",
    "⚰️": "caixao funerario funeral morte velorio",
    "🪦": "lapide cemiterio descanse paz morto rip sepultura tumba tumulo",
    "⚱️": "urna funeraria cinzas morte",
    "🧿": "olho grego amuleto conta mau-olhado micanga talisma",
    "🪬": "amuleto fatima guia mao maria miriam palma protecao sorte",
    "🗿": "moai estatua rosto",
    "🪧": "placa aviso cartaz demonstracao letreiro piquete protesto sinal",
    "🪪": "cartao identificacao carta carteira cracha credenciais documento habilitacao identidade licenca motorista rg seguranca",
    "🏧": "simbolo caixa automatico atm banco dinheiro eletronico grana saque",
    "🚮": "simbolo lixeira coloque lixo no lata",
    "🚰": "agua potavel beber simbolo torneira",
    "♿": "simbolo cadeira rodas acesso sinal",
    "🚹": "banheiro masculino homem lavatorio simbolo toalete wc",
    "🚺": "banheiro feminino lavabo lavatorio mulher simbolo toalete wc",
    "🚻": "banheiro lavabo sanitario simbolo toalete",
    "🚼": "simbolo bebe fralda fraldario trocar",
    "🚾": "wc latrina lavabo privada toalete vaso sanitario",
    "🛂": "controle passaportes passaporte",
    "🛃": "alfandega aduana aduaneira bens federal impostos receita",
    "🛄": "restituicao bagagem aeroporto area bagagens esteira ferias mala recolhimento viagem",
    "🛅": "deposito bagagem esquecida malas servico",
    "⚠️": "aviso atencao cuidado sinal",
    "🚸": "criancas atravessando crianca pedestre simbolo trafego",
    "⛔": "entrada proibida entre nao placa proibido sinal transito",
    "🚫": "proibido entrada nao proibida simbolo sinal",
    "🚳": "proibido andar bicicleta nao permitidas sem",
    "🚭": "proibido fumar cigarro nao permitido simbolo",
    "🚯": "proibido jogar lixo no chao nao jogue simbolo",
    "🚱": "agua nao potavel consumo impropria proibido seco sem",
    "🚷": "proibida passagem pedestres nao pedestre permitidos proibido",
    "📵": "proibido uso telefone celular cel nao permitidos sem smartphone",
    "🔞": "proibido menores 18 anos dezoito idade menor restricao",
    "☢️": "radioativo perigo radiacao radiativo simbolo sinal",
    "☣️": "risco biologico ciencia perigo residuos biologicos",
    "⬆️": "seta cima cardinal direcao norte",
    "↗️": "seta cima direita diagonal direcao flecha intercardinal nordeste superior",
    "➡️": "seta direita direcao flecha leste",
    "↘️": "seta baixo direita diagonal direcao flecha inferior intercardinal sudeste",
    "⬇️": "seta baixo abaixo cardinal direcao embaixo flecha sul",
    "↙️": "seta baixo esquerda diagonal direcao flecha inferior intercardinal sudoeste",
    "⬅️": "seta esquerda atras direcao flecha oeste voltar",
    "↖️": "seta cima esquerda diagonal superior direcao flecha intercardinal noroeste",
    "↕️": "seta cima baixo flecha vertical",
    "↔️": "seta esquerda direita flecha horizontal lados",
    "↩️": "seta curva direita esquerda flecha retorno voltar",
    "↪️": "seta curva esquerda direita flecha retorno voltar",
    "⤴️": "seta direita curvada cima curva flecha baixo",
    "⤵️": "seta direita curvada baixo curva embaixo flecha cima",
    "🔃": "setas verticais no sentido horario flechas recarregar seta na vertical simbolo",
    "🔄": "botao setas sentido anti-horario atualizar no seta",
    "🔙": "seta flecha esquerda voltar",
    "🔚": "seta fim esquerda final ir",
    "🔛": "seta flecha marca on! “on”",
    "🔜": "seta “soon” direita breve flecha simbolo",
    "🔝": "seta cima simbolo",
    "🛐": "local culto sagrado oracao religiao reza",
    "⚛️": "simbolo atomo ateismo ateu",
    "🕉️": "hindu religiao",
    "✡️": "estrela davi judaico judeu religiao",
    "☸️": "roda budista religiao",
    "☯️": "religiao tao taoista taoistas yin-yang",
    "✝️": "cruz latina cristao religiao",
    "☦️": "cruz ortodoxa cristao religiao",
    "☪️": "estrela lua crescente isla muculmano religiao simbolo",
    "☮️": "simbolo paz",
    "🕎": "menora candelabro judeu castical judaismo memorah religiao",
    "🔯": "estrela seis pontas adivinhacao destino",
    "🪯": "deg tegh fateh fe khalsa religiao sikh sikhismo siquismo",
    "♈": "signo carneiro zodiaco",
    "♉": "signo touro boi zodiaco",
    "♊": "signo gemeos zodiaco",
    "♋": "signo caranguejo zodiaco",
    "♌": "signo leao zodiaco",
    "♍": "signo virgem zodiaco",
    "♎": "signo balanca justica virgem zodiaco",
    "♏": "signo escorpiao zodiaco",
    "♐": "signo sagitario arqueiro zodiaco",
    "♑": "signo capricornio cabra zodiaco",
    "♒": "signo aquario agua zodiaco",
    "♓": "signo peixes zodiaco",
    "⛎": "signo ofiuco cobra serpente zodiaco",
    "🔀": "botao musicas aleatorias cruzadas flechas seta setas direcao direita",
    "🔁": "botao repetir flechas horario sentido seta setas",
    "🔂": "botao repetir unica faixa horario numero 1 vez sentido seta setas",
    "▶️": "botao reproduzir direita play seta triangulo",
    "⏩": "botao avancar direita dupla flechas passar frente rapido seta",
    "⏭️": "botao proxima faixa avancar flechas duplas passar frente cena seta dupla direita",
    "⏯️": "botao reproduzir ou pausar direita pause play seta triangulo",
    "◀️": "botao voltar esquerda seta triangulo",
    "⏪": "botao retroceder dupla esquerda seta tras voltar",
    "⏮️": "botao faixa anterior cena flechas esquerda seta dupla setas triangulo ultima voltar",
    "🔼": "botao apontando cima acima triangulo seta vermelho",
    "⏫": "botao avanco cima dupla flechas seta",
    "🔽": "botao apontando baixo triangulo seta vermelho",
    "⏬": "botao avanco baixo flechas retroceder rapido seta dupla",
    "⏸️": "botao pausar barra dupla pausado pause",
    "⏹️": "botao parar quadrado",
    "⏺️": "botao gravar circulo",
    "⏏️": "botao ejetar",
    "🎦": "entretenimento filme simbolo",
    "🔅": "botao diminuir brilho escurecer simbolo reduzir",
    "🔆": "botao aumentar brilho simbolo",
    "📶": "barras sinal antena celular conexao forca internet sinais telefonia movel telefone",
    "🛜": "sem fio banda larga computador conectividade internet ponto acesso rede roteador smartphone wi-fi wifi",
    "📳": "modo vibratorio cel celular smartphone telefone vibracao",
    "📴": "telefone celular desligado desligue smartphone",
    "♀️": "simbolo feminino mulher",
    "♂️": "simbolo masculino homem",
    "⚧️": "simbolo transgenero",
    "✖️": "sinal multiplicacao × cancelar multiplicar preto",
    "➕": "simbolo adicao + cruz mais matematica sinal maior somar",
    "➖": "simbolo subtracao - − diminuir matematica menos sinal travessao",
    "➗": "simbolo divisao ÷ dividir matematica sinal grande",
    "🟰": "sinal igual conta iguais igualdade matematica resposta resultado",
    "♾️": "infinito eternidade ilimitado universal",
    "‼️": "dupla exclamacao ! !! explosao ponto duplo pontuacao",
    "⁉️": "exclamacao interrogacao ! !? ? pergunta pontuacao sinal",
    "❓": "ponto interrogacao vermelho ? pergunta pontuacao sinal",
    "❔": "ponto interrogacao branco ? delineado pergunta pontuacao sinal",
    "❕": "ponto exclamacao branco ! delineado pontuacao sinal",
    "❗": "ponto exclamacao vermelho ! pontuacao",
    "〰️": "travessao ondulado onda pontuacao",
    "💱": "cambio moeda banco dinheiro",
    "💲": "cifrao dinheiro dolar grana moeda simbolo negrito",
    "⚕️": "simbolo medicina bastao asclepio esculapio",
    "♻️": "simbolo reciclagem reclicar solido sinal",
    "⚜️": "flor-de-lis cavaleiros simbolo",
    "🔱": "emblema tridente ancora ferramenta navio simbolo",
    "📛": "cracha identificacao nome",
    "🔰": "simbolo japones principiante folha verde amarela iniciante amarelo",
    "⭕": "circulo grande oco aro vermelho vazio",
    "✅": "marca selecao branca ✓ botao completo feito verificacao grande verificado",
    "☑️": "caixa selecao marcada tique ✓ feito marca verificacao cinza",
    "✔️": "marca selecao ✓ feito verificacao simples marcar selecionar",
    "❌": "xis × cancelar multiplicacao multiplicar vermelho x",
    "❎": "botao xis × x caixa marcado multiplicar quadrado verde",
    "➰": "laco encaracolado onda ondulado volta",
    "➿": "encaracolado duas vezes duplo encaradolado onda ondulado",
    "〽️": "sinal japones indicando inicio musica canto simbolo",
    "✳️": "asterisco oito pontas * estrela",
    "✴️": "estrela oito pontas *",
    "❇️": "faisca * cruz",
    "©️": "simbolo direitos autorais",
    "®️": "simbolo registrado marca registrada “r”",
    "™️": "simbolo marca registrada “tm”",
    "🫟": "respingo mancha pingo tinta",
    "#️⃣": "tecla #",
    "*️⃣": "tecla *",
    "0️⃣": "tecla 0",
    "1️⃣": "tecla 1",
    "2️⃣": "tecla 2 dois",
    "3️⃣": "tecla 3 tres",
    "4️⃣": "tecla 4 quatro",
    "5️⃣": "tecla 5 cinco",
    "6️⃣": "tecla 6 seis",
    "7️⃣": "tecla 7 sete",
    "8️⃣": "tecla 8 oito",
    "9️⃣": "tecla 9 nove",
    "🔟": "tecla 10",
    "🔠": "letras latinas maiusculas abcd caracteres latinos maiusculos digitacao inserir",
    "🔡": "letras latinas minusculas abcd caracteres latinos minusculos digitacao latino",
    "🔢": "numeros 1234 digitacao inserir",
    "🔣": "simbolos 〒♪&% digitacao inserir",
    "🔤": "letras latinas abc alfabeto digitacao ingles inserir latino",
    "🅰️": "botao (tipo sanguineo) sangue tipo sanguineo",
    "🆎": "botao (tipo sanguineo) sangue tipo sanguineo",
    "🅱️": "botao (tipo sanguineo) sangue tipo sanguineo",
    "🆑": "botao limpar simbolo",
    "🆒": "botao legal simbolo “cool”",
    "🆓": "botao gratis gratuito simbolo “free”",
    "ℹ️": "informacoes i informacao simbolo",
    "🆔": "botao identidade simbolo “id”",
    "Ⓜ️": "circulo letra",
    "🆕": "botao novo simbolo",
    "🆖": "botao simbolo “ng”",
    "🅾️": "botao (tipo sanguineo) sangue tipo sanguineo",
    "🆗": "botao sim simbolo “ok”",
    "🅿️": "botao estacionamento estacionar garagem carros",
    "🆘": "botao ajuda simbolo “sos” socorro",
    "🆙": "botao cima simbolo “up!” up!",
    "🆚": "botao simbolo “vs” versus",
    "🈁": "botao japones “aqui” alfabeto ココ",
    "🈂️": "botao japones “taxa servico” alfabeto katanaka taxa servico サ",
    "🈷️": "botao japones “quantidade mensal” alfabeto ideografico ideograma valor mensal 月",
    "🈶": "botao japones “nao gratuito” alfabeto ideografico ideograma nao gratuito 有",
    "🈯": "botao japones “reservado” alfabeto ideografico ideograma reservado 指",
    "🉐": "botao japones “barganha” alfabeto barganha ideografico ideograma pechincha 得",
    "🈹": "botao japones “desconto” alfabeto desconto ideografico ideograma 割",
    "🈚": "botao japones “gratuito” alfabeto graca gratis ideografico ideograma sem custo 無",
    "🈲": "botao japones “proibido” alfabeto ideografico quadrado “proibir” ideograma proibido proibir 禁",
    "🉑": "botao japones “aceitavel” aceitar alfabeto chines ideografico circular “aceitar” ideograma 可",
    "🈸": "botao japones “aplicacao” alfabeto aplicar chines ideografico quadrado “aplicar” ideograma 申",
    "🈴": "botao japones “nota minima” alfabeto chines ideografico quadrado “juntos” ideograma juntos 合",
    "🈳": "botao japones “vago” alfabeto ideografico ideograma vaga vago vazio 空",
    "㊗️": "botao japones “parabens” alfabeto ideografico circular ideograma janones parabens 祝",
    "㊙️": "botao japones “segredo” alfabeto ideografico circular ideograma segredo 秘",
    "🈺": "botao japones “aberto negocios” chines ideografico quadrado “operando” ideograma operando 営",
    "🈵": "botao japones “sem vagas” alfabeto completude ideografico quadrado “completude” ideograma 満",
    "🔴": "circulo vermelho bola vermelha grande geometrico",
    "🟠": "circulo laranja",
    "🟡": "circulo amarelo",
    "🟢": "circulo verde",
    "🔵": "circulo azul bolinha grande geometrico",
    "🟣": "circulo roxo",
    "🟤": "circulo marrom",
    "⚫": "circulo preto geometrico",
    "⚪": "circulo branco geometrico",
    "🟥": "quadrado vermelho",
    "🟧": "quadrado laranja",
    "🟨": "quadrado amarelo",
    "🟩": "quadrado verde",
    "🟦": "quadrado azul anil",
    "🟪": "quadrado roxo purpura",
    "🟫": "quadrado marrom",
    "⬛": "quadrado preto grande geometrico",
    "⬜": "quadrado branco grande geometrico",
    "◼️": "quadrado preto medio geometrico",
    "◻️": "quadrado branco medio geometrico",
    "◾": "quadrado preto medio menor geometrico",
    "◽": "quadrado branco medio menor geometrico",
    "▪️": "quadrado preto pequeno geometrico",
    "▫️": "quadrado branco pequeno geometrico",
    "🔶": "losango laranja grande cor diamante geometrico",
    "🔷": "losango azul grande balao diamante geometrico",
    "🔸": "losango laranja pequeno cor diamante geometrico",
    "🔹": "losango azul pequeno diamante geometrico",
    "🔺": "triangulo vermelho cima geometrico apontando",
    "🔻": "triangulo vermelho baixo geometrico apontando",
    "💠": "diamante ponto comico dentro formato geometrico",
    "🔘": "botao opcao geometrico",
    "🔳": "botao quadrado branco preto",
    "🔲": "botao quadrado preto branco geometrico",
    "🏁": "bandeira quadriculada chegada corrida esporte vitoria",
    "🚩": "bandeira informacoes localizacao poste vermelha vermelho",
    "🎌": "bandeiras cruzadas bandeira comemoracao cruzar japao japones",
    "🏴": "bandeira preta tremulando",
    "🏳️": "bandeira branca paz tremulando",
    "🏳️‍🌈": "bandeira arco-iris bissexual gay lesbica lgbt lgbtq lgbtqia orgulho transexual transgenero travesti",
    "🏳️‍⚧️": "bandeira transgenero azul claro branco rosa",
    "🏴‍☠️": "bandeira pirata caveira osso saque tesouro",
    "🇦🇨": "bandeira ilha ascensao",
    "🇦🇩": "bandeira andorra",
    "🇦🇪": "bandeira emirados arabes unidos",
    "🇦🇫": "bandeira afeganistao",
    "🇦🇬": "bandeira antigua barbuda",
    "🇦🇮": "bandeira anguila",
    "🇦🇱": "bandeira albania",
    "🇦🇲": "bandeira armenia",
    "🇦🇴": "bandeira angola",
    "🇦🇶": "bandeira antartida",
    "🇦🇷": "bandeira argentina",
    "🇦🇸": "bandeira samoa americana",
    "🇦🇹": "bandeira austria",
    "🇦🇺": "bandeira australia",
    "🇦🇼": "bandeira aruba",
    "🇦🇽": "bandeira ilhas aland",
    "🇦🇿": "bandeira azerbaijao",
    "🇧🇦": "bandeira bosnia herzegovina",
    "🇧🇧": "bandeira barbados",
    "🇧🇩": "bandeira bangladesh",
    "🇧🇪": "bandeira belgica",
    "🇧🇫": "bandeira burquina faso",
    "🇧🇬": "bandeira bulgaria",
    "🇧🇭": "bandeira barein",
    "🇧🇮": "bandeira burundi",
    "🇧🇯": "bandeira benin",
    "🇧🇱": "bandeira sao bartolomeu",
    "🇧🇲": "bandeira bermudas",
    "🇧🇳": "bandeira brunei",
    "🇧🇴": "bandeira bolivia",
    "🇧🇶": "bandeira paises baixos caribenhos",
    "🇧🇷": "bandeira brasil",
    "🇧🇸": "bandeira bahamas",
    "🇧🇹": "bandeira butao",
    "🇧🇻": "bandeira ilha bouvet",
    "🇧🇼": "bandeira botsuana",
    "🇧🇾": "bandeira bielorrussia",
    "🇧🇿": "bandeira belize",
    "🇨🇦": "bandeira canada",
    "🇨🇨": "bandeira ilhas cocos (keeling)",
    "🇨🇩": "bandeira congo - kinshasa",
    "🇨🇫": "bandeira republica centro-africana",
    "🇨🇬": "bandeira republica congo",
    "🇨🇭": "bandeira suica",
    "🇨🇮": "bandeira costa marfim",
    "🇨🇰": "bandeira ilhas cook",
    "🇨🇱": "bandeira chile",
    "🇨🇲": "bandeira camaroes",
    "🇨🇳": "bandeira china",
    "🇨🇴": "bandeira colombia",
    "🇨🇵": "bandeira ilha clipperton",
    "🇨🇶": "bandeira sark",
    "🇨🇷": "bandeira costa rica",
    "🇨🇺": "bandeira cuba",
    "🇨🇻": "bandeira cabo verde",
    "🇨🇼": "bandeira curacao",
    "🇨🇽": "bandeira ilha christmas",
    "🇨🇾": "bandeira chipre",
    "🇨🇿": "bandeira tchequia",
    "🇩🇪": "bandeira alemanha",
    "🇩🇬": "bandeira diego garcia",
    "🇩🇯": "bandeira djibuti",
    "🇩🇰": "bandeira dinamarca",
    "🇩🇲": "bandeira dominica",
    "🇩🇴": "bandeira republica dominicana",
    "🇩🇿": "bandeira argelia",
    "🇪🇦": "bandeira ceuta melilla",
    "🇪🇨": "bandeira equador",
    "🇪🇪": "bandeira estonia",
    "🇪🇬": "bandeira egito",
    "🇪🇭": "bandeira saara ocidental",
    "🇪🇷": "bandeira eritreia",
    "🇪🇸": "bandeira espanha",
    "🇪🇹": "bandeira etiopia",
    "🇪🇺": "bandeira uniao europeia",
    "🇫🇮": "bandeira finlandia",
    "🇫🇯": "bandeira fiji",
    "🇫🇰": "bandeira ilhas malvinas",
    "🇫🇲": "bandeira micronesia",
    "🇫🇴": "bandeira ilhas faroe",
    "🇫🇷": "bandeira franca",
    "🇬🇦": "bandeira gabao",
    "🇬🇧": "bandeira reino unido",
    "🇬🇩": "bandeira granada",
    "🇬🇪": "bandeira georgia",
    "🇬🇫": "bandeira guiana francesa",
    "🇬🇬": "bandeira guernsey",
    "🇬🇭": "bandeira gana",
    "🇬🇮": "bandeira gibraltar",
    "🇬🇱": "bandeira groenlandia",
    "🇬🇲": "bandeira gambia",
    "🇬🇳": "bandeira guine",
    "🇬🇵": "bandeira guadalupe",
    "🇬🇶": "bandeira guine equatorial",
    "🇬🇷": "bandeira grecia",
    "🇬🇸": "bandeira ilhas georgia sul sandwich",
    "🇬🇹": "bandeira guatemala",
    "🇬🇺": "bandeira guam",
    "🇬🇼": "bandeira guine-bissau",
    "🇬🇾": "bandeira guiana",
    "🇭🇰": "bandeira hong kong rae china",
    "🇭🇲": "bandeira ilhas heard mcdonald",
    "🇭🇳": "bandeira honduras",
    "🇭🇷": "bandeira croacia",
    "🇭🇹": "bandeira haiti",
    "🇭🇺": "bandeira hungria",
    "🇮🇨": "bandeira ilhas canarias",
    "🇮🇩": "bandeira indonesia",
    "🇮🇪": "bandeira irlanda",
    "🇮🇱": "bandeira israel",
    "🇮🇲": "bandeira ilha man",
    "🇮🇳": "bandeira india",
    "🇮🇴": "bandeira territorio britanico oceano indico",
    "🇮🇶": "bandeira iraque",
    "🇮🇷": "bandeira ira",
    "🇮🇸": "bandeira islandia",
    "🇮🇹": "bandeira italia",
    "🇯🇪": "bandeira jersey",
    "🇯🇲": "bandeira jamaica",
    "🇯🇴": "bandeira jordania",
    "🇯🇵": "bandeira japao",
    "🇰🇪": "bandeira quenia",
    "🇰🇬": "bandeira quirguistao",
    "🇰🇭": "bandeira camboja",
    "🇰🇮": "bandeira quiribati",
    "🇰🇲": "bandeira comores",
    "🇰🇳": "bandeira sao cristovao nevis",
    "🇰🇵": "bandeira coreia norte",
    "🇰🇷": "bandeira coreia sul",
    "🇰🇼": "bandeira kuwait",
    "🇰🇾": "bandeira ilhas cayman",
    "🇰🇿": "bandeira cazaquistao",
    "🇱🇦": "bandeira laos",
    "🇱🇧": "bandeira libano",
    "🇱🇨": "bandeira santa lucia",
    "🇱🇮": "bandeira liechtenstein",
    "🇱🇰": "bandeira sri lanka",
    "🇱🇷": "bandeira liberia",
    "🇱🇸": "bandeira lesoto",
    "🇱🇹": "bandeira lituania",
    "🇱🇺": "bandeira luxemburgo",
    "🇱🇻": "bandeira letonia",
    "🇱🇾": "bandeira libia",
    "🇲🇦": "bandeira marrocos",
    "🇲🇨": "bandeira monaco",
    "🇲🇩": "bandeira moldavia",
    "🇲🇪": "bandeira montenegro",
    "🇲🇫": "bandeira sao martinho",
    "🇲🇬": "bandeira madagascar",
    "🇲🇭": "bandeira ilhas marshall",
    "🇲🇰": "bandeira macedonia norte",
    "🇲🇱": "bandeira mali",
    "🇲🇲": "bandeira mianmar (birmania)",
    "🇲🇳": "bandeira mongolia",
    "🇲🇴": "bandeira macau rae china",
    "🇲🇵": "bandeira ilhas marianas norte",
    "🇲🇶": "bandeira martinica",
    "🇲🇷": "bandeira mauritania",
    "🇲🇸": "bandeira montserrat",
    "🇲🇹": "bandeira malta",
    "🇲🇺": "bandeira mauricio",
    "🇲🇻": "bandeira maldivas",
    "🇲🇼": "bandeira malaui",
    "🇲🇽": "bandeira mexico",
    "🇲🇾": "bandeira malasia",
    "🇲🇿": "bandeira mocambique",
    "🇳🇦": "bandeira namibia",
    "🇳🇨": "bandeira nova caledonia",
    "🇳🇪": "bandeira niger",
    "🇳🇫": "bandeira ilha norfolk",
    "🇳🇬": "bandeira nigeria",
    "🇳🇮": "bandeira nicaragua",
    "🇳🇱": "bandeira paises baixos",
    "🇳🇴": "bandeira noruega",
    "🇳🇵": "bandeira nepal",
    "🇳🇷": "bandeira nauru",
    "🇳🇺": "bandeira niue",
    "🇳🇿": "bandeira nova zelandia",
    "🇴🇲": "bandeira oma",
    "🇵🇦": "bandeira panama",
    "🇵🇪": "bandeira peru",
    "🇵🇫": "bandeira polinesia francesa",
    "🇵🇬": "bandeira papua-nova guine",
    "🇵🇭": "bandeira filipinas",
    "🇵🇰": "bandeira paquistao",
    "🇵🇱": "bandeira polonia",
    "🇵🇲": "bandeira sao pedro miquelao",
    "🇵🇳": "bandeira ilhas pitcairn",
    "🇵🇷": "bandeira porto rico",
    "🇵🇸": "bandeira territorios palestinos",
    "🇵🇹": "bandeira portugal",
    "🇵🇼": "bandeira palau",
    "🇵🇾": "bandeira paraguai",
    "🇶🇦": "bandeira catar",
    "🇷🇪": "bandeira reuniao",
    "🇷🇴": "bandeira romenia",
    "🇷🇸": "bandeira servia",
    "🇷🇺": "bandeira russia",
    "🇷🇼": "bandeira ruanda",
    "🇸🇦": "bandeira arabia saudita",
    "🇸🇧": "bandeira ilhas salomao",
    "🇸🇨": "bandeira seicheles",
    "🇸🇩": "bandeira sudao",
    "🇸🇪": "bandeira suecia",
    "🇸🇬": "bandeira singapura",
    "🇸🇭": "bandeira santa helena",
    "🇸🇮": "bandeira eslovenia",
    "🇸🇯": "bandeira svalbard jan mayen",
    "🇸🇰": "bandeira eslovaquia",
    "🇸🇱": "bandeira serra leoa",
    "🇸🇲": "bandeira san marino",
    "🇸🇳": "bandeira senegal",
    "🇸🇴": "bandeira somalia",
    "🇸🇷": "bandeira suriname",
    "🇸🇸": "bandeira sudao sul",
    "🇸🇹": "bandeira sao tome principe",
    "🇸🇻": "bandeira salvador",
    "🇸🇽": "bandeira sint maarten",
    "🇸🇾": "bandeira siria",
    "🇸🇿": "bandeira essuatini",
    "🇹🇦": "bandeira tristao cunha",
    "🇹🇨": "bandeira ilhas turcas caicos",
    "🇹🇩": "bandeira chade",
    "🇹🇫": "bandeira territorios franceses sul",
    "🇹🇬": "bandeira togo",
    "🇹🇭": "bandeira tailandia",
    "🇹🇯": "bandeira tadjiquistao",
    "🇹🇰": "bandeira tokelau",
    "🇹🇱": "bandeira timor-leste",
    "🇹🇲": "bandeira turcomenistao",
    "🇹🇳": "bandeira tunisia",
    "🇹🇴": "bandeira tonga",
    "🇹🇷": "bandeira turquia",
    "🇹🇹": "bandeira trinidad tobago",
    "🇹🇻": "bandeira tuvalu",
    "🇹🇼": "bandeira taiwan",
    "🇹🇿": "bandeira tanzania",
    "🇺🇦": "bandeira ucrania",
    "🇺🇬": "bandeira uganda",
    "🇺🇲": "bandeira ilhas menores distantes eua",
    "🇺🇳": "bandeira nacoes unidas",
    "🇺🇸": "bandeira estados unidos",
    "🇺🇾": "bandeira uruguai",
    "🇺🇿": "bandeira uzbequistao",
    "🇻🇦": "bandeira cidade vaticano",
    "🇻🇨": "bandeira sao vicente granadinas",
    "🇻🇪": "bandeira venezuela",
    "🇻🇬": "bandeira ilhas virgens britanicas",
    "🇻🇮": "bandeira ilhas virgens americanas",
    "🇻🇳": "bandeira vietna",
    "🇻🇺": "bandeira vanuatu",
    "🇼🇫": "bandeira wallis futuna",
    "🇼🇸": "bandeira samoa",
    "🇽🇰": "bandeira kosovo",
    "🇾🇪": "bandeira iemen",
    "🇾🇹": "bandeira mayotte",
    "🇿🇦": "bandeira africa sul",
    "🇿🇲": "bandeira zambia",
    "🇿🇼": "bandeira zimbabue",
    "🏴󠁧󠁢󠁥󠁮󠁧󠁿": "bandeira inglaterra",
    "🏴󠁧󠁢󠁳󠁣󠁴󠁿": "bandeira escocia",
    "🏴󠁧󠁢󠁷󠁬󠁳󠁿": "bandeira pais gales",
}
# <<< EMOJI_CLDR_KEYWORDS

# Search stays multilingual regardless of the selected UI language, matching
# the established aliases above: a Turkish user may search for "mavi kalp"
# without losing the existing English "blue heart" lookup.  The generated
# module comes from the same Unicode CLDR release as the picker data.
EMOJI_LOCALIZED_KEYWORD_INDEXES = (
    EMOJI_CLDR_KEYWORDS,
    EMOJI_CLDR_KEYWORDS_TR,
)

# Connector words describe the phrase, not the emoji. Ignoring them lets
# natural searches such as "fone de ouvido", "bandeira do Brasil" and
# "emoji de coração" validate against the meaningful terms.
SEARCH_STOP_WORDS = {
    "a", "as", "com", "da", "das", "de", "do", "dos", "e", "em",
    "emoji", "emogi", "o", "os", "para", "por", "pra", "pro", "que",
    "se", "um", "uma",
    "and", "for", "of", "the", "with",
    "con", "del", "el", "la", "las", "los", "y",
    "bir", "icin", "ile", "ve",
}


def _search_text(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value or "")
    # Turkish "ı" is its own letter to Unicode, so neither NFKD nor casefold()
    # turns it into "i" -- and casefold() lowers "I" to "i". Folding it here
    # lets "kirmizi" and "KIRMIZI" find the CLDR term "kırmızı".
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()
    return folded.replace("ı", "i")


def _unicode_name(emoji: str) -> str:
    return " ".join(
        unicodedata.name(ch, "")
        for ch in emoji
        if ch not in ("\ufe0f", "\u200d") and unicodedata.name(ch, "")
    )


# Skin tone variants sit beside their base emoji in the CLDR ordering, so the
# picker derives the ladders at import time instead of shipping extra data.
_SKIN_TONES = (
    "\U0001F3FB", "\U0001F3FC", "\U0001F3FD", "\U0001F3FE", "\U0001F3FF",
)


def _skin_tone_rank(emoji: str) -> tuple[int, ...]:
    return tuple(_SKIN_TONES.index(ch) for ch in emoji if ch in _SKIN_TONES)


def _base_emoji(emoji: str) -> str:
    """Toneless grouping key.

    Drops the presentation selector too: the CLDR lists these bases with
    U+FE0F but their toned variants without it ("U+1F590 U+FE0F" vs
    "U+1F590 U+1F3FB"), which would otherwise split one ladder in two.
    """
    return "".join(
        ch for ch in emoji
        if ch not in _SKIN_TONES and ch != "\uFE0F"
    )


# Same concept, multiple official encodings: a compact single-codepoint emoji
# plus ZWJ sequences that carry the mixed skin tones.  The picker folds every
# long family into the compact emoji and shows a single row per concept.
_TWIN_FORMS = {
    "\U0001F91D": ("\U0001FAF1\u200D\U0001FAF2",),  # handshake
    "\U0001F46B": ("\U0001F469\u200D\U0001F91D\u200D\U0001F468",),  # holding hands
    "\U0001F46C": ("\U0001F468\u200D\U0001F91D\u200D\U0001F468",),
    "\U0001F46D": ("\U0001F469\u200D\U0001F91D\u200D\U0001F469",),
    "\U0001F491": (  # couple with heart
        "\U0001F469\u200D\u2764\uFE0F\u200D\U0001F468",
        "\U0001F469\u200D\u2764\uFE0F\u200D\U0001F469",
        "\U0001F468\u200D\u2764\uFE0F\u200D\U0001F468",
        "\U0001F9D1\u200D\u2764\uFE0F\u200D\U0001F9D1",
    ),
    "\U0001F48F": (  # kiss
        "\U0001F469\u200D\u2764\uFE0F\u200D\U0001F48B\u200D\U0001F468",
        "\U0001F469\u200D\u2764\uFE0F\u200D\U0001F48B\u200D\U0001F469",
        "\U0001F468\u200D\u2764\uFE0F\u200D\U0001F48B\u200D\U0001F468",
        "\U0001F9D1\u200D\u2764\uFE0F\u200D\U0001F48B\u200D\U0001F9D1",
    ),
    "\U0001F46A": (  # family: every composition rides one row
        "\U0001F468\u200D\U0001F469\u200D\U0001F466",
        "\U0001F468\u200D\U0001F469\u200D\U0001F467",
        "\U0001F468\u200D\U0001F469\u200D\U0001F467\u200D\U0001F466",
        "\U0001F468\u200D\U0001F469\u200D\U0001F466\u200D\U0001F466",
        "\U0001F468\u200D\U0001F469\u200D\U0001F467\u200D\U0001F467",
        "\U0001F468\u200D\U0001F468\u200D\U0001F466",
        "\U0001F468\u200D\U0001F468\u200D\U0001F467",
        "\U0001F468\u200D\U0001F468\u200D\U0001F467\u200D\U0001F466",
        "\U0001F468\u200D\U0001F468\u200D\U0001F466\u200D\U0001F466",
        "\U0001F468\u200D\U0001F468\u200D\U0001F467\u200D\U0001F467",
        "\U0001F469\u200D\U0001F469\u200D\U0001F466",
        "\U0001F469\u200D\U0001F469\u200D\U0001F467",
        "\U0001F469\u200D\U0001F469\u200D\U0001F467\u200D\U0001F466",
        "\U0001F469\u200D\U0001F469\u200D\U0001F466\u200D\U0001F466",
        "\U0001F469\u200D\U0001F469\u200D\U0001F467\u200D\U0001F467",
        "\U0001F468\u200D\U0001F466",
        "\U0001F468\u200D\U0001F466\u200D\U0001F466",
        "\U0001F468\u200D\U0001F467",
        "\U0001F468\u200D\U0001F467\u200D\U0001F466",
        "\U0001F468\u200D\U0001F467\u200D\U0001F467",
        "\U0001F469\u200D\U0001F466",
        "\U0001F469\u200D\U0001F466\u200D\U0001F466",
        "\U0001F469\u200D\U0001F467",
        "\U0001F469\u200D\U0001F467\u200D\U0001F466",
        "\U0001F469\u200D\U0001F467\u200D\U0001F467",
        "\U0001F9D1\u200D\U0001F9D2",
        "\U0001F9D1\u200D\U0001F9D2\u200D\U0001F9D2",
        "\U0001F9D1\u200D\U0001F9D1\u200D\U0001F9D2",
        "\U0001F9D1\u200D\U0001F9D1\u200D\U0001F9D2\u200D\U0001F9D2",
    ),
    "\U0001F46F": ("\U0001F9D1\u200D\U0001F430\u200D\U0001F9D1",),  # bunny ears
    "\U0001F46F\u200D\u2642\uFE0F": ("\U0001F468\u200D\U0001F430\u200D\U0001F468",),
    "\U0001F46F\u200D\u2640\uFE0F": ("\U0001F469\u200D\U0001F430\u200D\U0001F469",),
    "\U0001F93C": ("\U0001F9D1\u200D\U0001FAEF\u200D\U0001F9D1",),  # wrestling
    "\U0001F93C\u200D\u2642\uFE0F": ("\U0001F468\u200D\U0001FAEF\u200D\U0001F468",),
    "\U0001F93C\u200D\u2640\uFE0F": ("\U0001F469\u200D\U0001FAEF\u200D\U0001F469",),
}


def _variants_by_base() -> dict[str, tuple[str, ...]]:
    """Group every emoji sharing a toneless base, ordered light to dark."""
    grouped: dict[str, list[tuple[tuple[int, ...], str]]] = {}
    canonical: dict[str, str] = {}
    seen: set[str] = set()
    for _key, values in EMOJI_CATEGORIES:
        for emoji in values.split():
            if emoji in seen:
                continue
            seen.add(emoji)
            base = _base_emoji(emoji)
            grouped.setdefault(base, []).append((_skin_tone_rank(emoji), emoji))

    for short, longs in _TWIN_FORMS.items():
        for long in longs:
            members = grouped.pop(_base_emoji(long), None)
            if members:
                short_base = _base_emoji(short)
                grouped.setdefault(short_base, []).extend(members)
                canonical[short_base] = short

    families: dict[str, tuple[str, ...]] = {}
    for base, members in grouped.items():
        if len(members) < 2:
            continue
        canon = canonical.get(base)
        ordered = sorted(
            members, key=lambda pair: (pair[1] != canon, pair[0], pair[1])
        )
        families[base] = tuple(emoji for _rank, emoji in ordered)
    return families


EMOJI_VARIANTS = _variants_by_base()

# Absorbed long spellings resolve to their compact twin's family.
_TWIN_TARGET = {
    _base_emoji(long): _base_emoji(short)
    for short, longs in _TWIN_FORMS.items()
    for long in longs
}

_HIDDEN_TWINS = frozenset(
    long for longs in _TWIN_FORMS.values() for long in longs
)


def _family_of(emoji: str) -> tuple[str, ...]:
    base = _base_emoji(emoji)
    return EMOJI_VARIANTS.get(_TWIN_TARGET.get(base, base)) or ()


def _is_variant_row(emoji: str) -> bool:
    """True for members hidden behind their family's first row."""
    if emoji in _HIDDEN_TWINS:
        return True
    variants = _family_of(emoji)
    return len(variants) > 1 and emoji != variants[0]


def _aliases_by_emoji() -> dict[str, str]:
    """Merge, rather than overwrite, every keyword group for an emoji."""
    merged: dict[str, list[str]] = {}
    for emoji_group, terms in EMOJI_SEARCH_ALIASES.items():
        for emoji in emoji_group.split():
            merged.setdefault(emoji, []).append(terms)
    return {emoji: " ".join(groups) for emoji, groups in merged.items()}


# Typo scoring is the hot path of every keystroke; memoize pair verdicts and
# skip pairs whose length gap alone caps their similarity under threshold.
_FUZZY_VERDICTS: dict[tuple[str, str], bool] = {}
_FUZZY_MIN_THRESHOLD = 0.72


def _fuzzy_match(needle: str, candidate: str) -> bool:
    key = (needle, candidate)
    verdict = _FUZZY_VERDICTS.get(key)
    if verdict is None:
        shared = 0
        for a, b in zip(needle, candidate):
            if a != b:
                break
            shared += 1
        threshold = _FUZZY_MIN_THRESHOLD if shared >= 4 else 0.82
        verdict = SequenceMatcher(None, needle, candidate).ratio() >= threshold
        if len(_FUZZY_VERDICTS) < 100_000:
            _FUZZY_VERDICTS[key] = verdict
    return verdict


def _word_matches(needle: str, candidate: str) -> bool:
    """Accent-insensitive word matching with conservative typo tolerance."""
    if not needle or not candidate:
        return False
    if needle == candidate:
        return True
    # Prefix/substring matching makes singulars, plurals and partial terms
    # useful ("tartar" -> "tartaruga") without fuzzy-matching tiny words.
    if min(len(needle), len(candidate)) >= 3 and needle in candidate:
        return True
    # A trailing plural "s" hides the singular ("cavalos" ~ "cavalo").
    stem_needle = needle[:-1] if len(needle) >= 4 and needle.endswith("s") else needle
    stem_candidate = (
        candidate[:-1] if len(candidate) >= 4 and candidate.endswith("s")
        else candidate
    )
    if (stem_needle, stem_candidate) != (needle, candidate):
        if stem_needle == stem_candidate or (
            len(stem_needle) >= 3 and stem_needle in stem_candidate
        ):
            return True
        needle, candidate = stem_needle, stem_candidate
    if len(needle) >= 5 and len(candidate) >= 5:
        shorter, longer = sorted((len(needle), len(candidate)))
        # ratio <= 2*shorter/(shorter+longer): reject hopeless pairs cheaply.
        if shorter * (200 - 72) < longer * 72:
            return False
        return _fuzzy_match(needle, candidate)
    return False


def _build_search_index() -> tuple[dict[str, tuple[str, ...]],
                                   dict[str, tuple[str, ...]]]:
    """Pre-normalized word buckets per displayed row, built once at import.

    Every row's bucket also absorbs its family members' names and keywords,
    so hidden spellings (skin tones, gender forms, family compositions)
    remain findable through the single row that represents them.

    Two buckets, not one. The first is everything a row can be found by. The
    second holds only what the row IS — its Unicode name and curated aliases —
    leaving out the CLDR keywords, which describe what a row is merely
    associated with. Both are needed because those are different claims:
    "cachorro" is a name of 🐶 and an association of 🦴 (bone), and with a
    single flat bucket the two were indistinguishable, so searching for
    "cachorro" answered with the bone — whichever category happened to come
    first won. Matching still uses everything; only the ordering reads this.
    """
    alias_map = _aliases_by_emoji()
    words_by_row: dict[str, set[str]] = {}
    names_by_row: dict[str, set[str]] = {}

    def add(bucket_map: dict[str, set[str]], rep: str, *texts: str) -> None:
        bucket = bucket_map.setdefault(rep, set())
        for text in texts:
            if text:
                bucket.update(_search_text(text).split())

    seen: set[str] = set()
    for _key, values in EMOJI_CATEGORIES:
        for emoji in values.split():
            if emoji in seen:
                continue
            seen.add(emoji)
            family = _family_of(emoji)
            rep = family[0] if family else emoji
            own_name = emoji if emoji == rep else ""
            localized_keywords = tuple(
                index.get(emoji, "")
                for index in EMOJI_LOCALIZED_KEYWORD_INDEXES
            )
            add(
                words_by_row,
                rep,
                own_name,
                _unicode_name(emoji),
                alias_map.get(emoji, ""),
                *localized_keywords,
            )
            add(
                names_by_row,
                rep,
                own_name,
                _unicode_name(emoji),
                alias_map.get(emoji, ""),
            )
    return (
        {row: tuple(words) for row, words in words_by_row.items()},
        {row: tuple(words) for row, words in names_by_row.items()},
    )


_SEARCH_INDEX, _NAME_INDEX = _build_search_index()


def _closeness(query_words: list[str], candidates: tuple[str, ...]) -> float:
    """How well a row matched, for ordering rows that only matched fuzzily.

    Each query word scores its best similarity against any of the row's words;
    the row scores the weakest of those, so a row cannot ride one strong word
    while another barely scraped through. Without this, typo tolerance
    returned its hits in category order: "cachoro" answered 😢 (choro) ahead of
    🐶 (cachorro), even though one is a single missing letter away and the
    other is two edits and a different word.
    """
    scores = []
    for word in query_words:
        best = 0.0
        for candidate in candidates:
            if not candidate:
                continue
            ratio = SequenceMatcher(None, word, candidate).ratio()
            if ratio > best:
                best = ratio
                if best == 1.0:
                    break
        scores.append(best)
    return min(scores) if scores else 0.0


def filter_emojis(query: str, category_index: int, category_labels) -> list[str]:
    """Return one category, or matching emoji from every category when searching.

    Skin tone variants collapse into their base row; the Left/Right keys walk
    each row's tone ladder, so listing them separately would only duplicate
    navigation targets.
    """
    if not query.strip():
        values = EMOJI_CATEGORIES[category_index][1].split()
        return [e for e in values if not _is_variant_row(e)]

    needle = _search_text(query.strip())
    query_words = [word for word in needle.split() if word not in SEARCH_STOP_WORDS]
    if not query_words:
        return []
    # Four tiers, best first. The old code had two and computed the first
    # without ever using it: `direct_emoji` was calculated and then guarded the
    # whole body with `if not direct_emoji`, so pasting an emoji to look it up
    # skipped that row entirely and the search answered with nothing at all.
    named: list[str] = []       # the query IS this row's name or alias
    associated: list[str] = []  # verbatim match, but only among CLDR keywords
    fuzzy: list[tuple[float, int, str]] = []  # typo/inflection, best first
    seen = set()
    for index, (_key, values) in enumerate(EMOJI_CATEGORIES):
        label_words = tuple(_search_text(category_labels[index]).split())
        for emoji in values.split():
            if _is_variant_row(emoji) or emoji in seen:
                continue
            seen.add(emoji)
            words = _SEARCH_INDEX.get(emoji, ()) + label_words
            if needle == _search_text(emoji):
                # The row the user pasted: nothing can outrank it.
                named.insert(0, emoji)
                continue
            word_set = frozenset(words)
            # Rows containing every query word verbatim outrank rows that only
            # matched through typo/inflection tolerance.
            if all(word in word_set for word in query_words):
                name_set = frozenset(_NAME_INDEX.get(emoji, ()))
                if all(word in name_set for word in query_words):
                    named.append(emoji)
                else:
                    associated.append(emoji)
                continue
            if not all(
                any(_word_matches(word, candidate) for candidate in words)
                for word in query_words
            ):
                continue
            # Name closeness leads, for the same reason the verbatim tiers
            # split: a typo of a row's own name beats an equally close typo of
            # something a row is merely associated with. "cachoro" scores an
            # identical 0.93 against 🐶 and against 🦴 — both hold the word
            # "cachorro" — and only this separates them, because for the bone
            # it is a CLDR association while for the dog it is its name.
            # Negated so the plain tuple sort puts the closest first; the index
            # keeps rows of equal closeness in their original category order.
            fuzzy.append((
                -_closeness(query_words, _NAME_INDEX.get(emoji, ())),
                -_closeness(query_words, words),
                len(fuzzy),
                emoji,
            ))
    fuzzy.sort()
    return named + associated + [row[-1] for row in fuzzy]


def insert_emoji(text_ctrl, emoji: str) -> bool:
    """Insert at the native selection and restore focus.

    WriteText delegates selection replacement and UTF-16 caret accounting to
    wx/Windows, avoiding broken caret positions around existing emoji.
    """
    if not emoji:
        return False
    text_ctrl.WriteText(emoji)
    text_ctrl.SetFocus()
    return True


class EmojiPickerDialog(wx.Dialog):
    def __init__(self, parent, i18n, *, reaction_mode: bool = False):
        title = "react_dialog_title" if reaction_mode else "emoji_picker_title"
        super().__init__(parent, title=i18n.t(title), size=(420, 480))
        self._i18n = i18n
        self._reaction_mode = reaction_mode
        self._selected_emoji = ""
        self._queued_emojis: list[str] = []
        main_window = getattr(parent, "main_window", None)
        self._announce = getattr(main_window, "output", None)

        panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)
        hint_key = "emoji_picker_reaction_hint" if reaction_mode else "emoji_picker_hint"
        hint = wx.StaticText(panel, label=i18n.t(hint_key))
        sizer.Add(hint, 0, wx.EXPAND | wx.ALL, 8)

        self._search_button = wx.Button(panel, label=i18n.t("emoji_picker_search"))
        self._search_button.SetName(i18n.t("emoji_picker_search").replace("&", ""))
        self._search_button.Bind(wx.EVT_BUTTON, self._on_search_button)
        sizer.Add(self._search_button, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._search = wx.SearchCtrl(panel, style=wx.TE_PROCESS_ENTER)
        # SetDescriptiveText is only a visual placeholder on Windows; it does
        # not reliably become the accessible name announced by NVDA/JAWS.
        # SetName gives the edit field an explicit role description.
        self._search.SetName(i18n.t("emoji_picker_search").replace("&", ""))
        self._search.SetDescriptiveText(i18n.t("emoji_picker_search_hint"))
        self._search.SetHelpText(i18n.t("emoji_picker_search_hint"))
        # Keep the edit out of the initial Tab order. Screen-reader users first
        # encounter the explicit native button above; activating it enables
        # and focuses this field, making the search interaction intentional
        # instead of adding an unexplained edit box to normal category/list
        # navigation.
        self._search.Disable()
        self._search.Bind(wx.EVT_TEXT, self._on_search_changed)
        self._search.Bind(wx.EVT_TEXT_ENTER, self._on_search_enter)
        sizer.Add(self._search, 0, wx.EXPAND | wx.ALL, 8)

        self._result_status = wx.StaticText(panel, label="")
        self._result_status.SetName(i18n.t("emoji_picker_search_results_label"))
        sizer.Add(self._result_status, 0, wx.EXPAND | wx.LEFT | wx.RIGHT | wx.BOTTOM, 8)

        category_label = wx.StaticText(panel, label=i18n.t("emoji_picker_category"))
        sizer.Add(category_label, 0, wx.LEFT | wx.RIGHT | wx.TOP, 8)
        self._category = wx.Choice(
            panel,
            choices=[i18n.t(key) for key, _ in EMOJI_CATEGORIES],
        )
        self._category.SetSelection(0)
        self._category.Bind(wx.EVT_CHOICE, self._on_category_changed)
        sizer.Add(self._category, 0, wx.EXPAND | wx.ALL, 8)

        self._list = wx.ListCtrl(panel, style=wx.LC_REPORT | wx.LC_SINGLE_SEL)
        self._list.InsertColumn(0, i18n.t("emoji_picker_emoji"), width=340)
        self._list.Bind(wx.EVT_LIST_ITEM_ACTIVATED, self._on_activated)
        self._list.Bind(wx.EVT_LIST_ITEM_SELECTED, self._on_selected)
        # Catch Ctrl+Enter before Windows turns Enter into the native
        # EVT_LIST_ITEM_ACTIVATED notification. A normal Enter is skipped and
        # keeps the established one-emoji insert-and-close path untouched.
        self.Bind(wx.EVT_CHAR_HOOK, self._on_char_hook)
        sizer.Add(self._list, 1, wx.EXPAND | wx.LEFT | wx.RIGHT, 8)

        buttons = wx.StdDialogButtonSizer()
        ok_key = "react_to_message" if reaction_mode else "emoji_picker_insert"
        insert_btn = wx.Button(panel, wx.ID_OK, i18n.t(ok_key))
        cancel_btn = wx.Button(panel, wx.ID_CANCEL, i18n.t("cancel"))
        insert_btn.SetDefault()
        buttons.AddButton(insert_btn)
        buttons.AddButton(cancel_btn)
        buttons.Realize()
        sizer.Add(buttons, 0, wx.ALIGN_RIGHT | wx.ALL, 8)

        panel.SetSizer(sizer)
        self._populate()
        self.Bind(wx.EVT_BUTTON, self._on_ok, id=wx.ID_OK)
        self.CentreOnParent()
        self._search_button.SetFocus()

    def _on_search_button(self, event):
        """Make the search field discoverable through a real native button."""
        self._search.Enable()
        self._search.SetFocus()
        self._search.SelectAll()

    def _populate(self):
        query = self._search.GetValue()
        self._list.DeleteAllItems()
        labels = [self._i18n.t(key) for key, _ in EMOJI_CATEGORIES]
        emojis = filter_emojis(query, self._category.GetSelection(), labels)
        for emoji in emojis:
            self._list.Append((emoji,))
        self._selected_emoji = emojis[0] if emojis else ""
        if emojis:
            result_text = self._i18n.t("emoji_picker_search_results").format(
                count=len(emojis)
            )
        else:
            result_text = self._i18n.t("emoji_picker_search_no_results")
        self._result_status.SetLabel(result_text)

        # Never move focus to the result list from inside EVT_TEXT. On native
        # Windows controls that steals the remainder of the current keyboard
        # input, which made the field keep only the first typed character.
        # Empty/category navigation may still select the first list item; a
        # search moves there only after the user validates it with Enter.
        if emojis:
            if not query.strip():
                self._list.Focus(0)
                self._list.Select(0)

    def _on_category_changed(self, event):
        self._populate()

    def _on_search_changed(self, event):
        self._populate()

    def _on_search_enter(self, event):
        if self._list.GetItemCount() > 0:
            self._list.Focus(0)
            self._list.Select(0)
            self._list.SetFocus()
        else:
            self._selected_emoji = ""
            wx.Bell()
            self._search.SetFocus()
            self._search.SelectAll()

    def _on_selected(self, event):
        index = event.GetIndex()
        if index >= 0:
            self._selected_emoji = self._list.GetItemText(index)

    def _current_emoji(self) -> str:
        index = self._list.GetFirstSelected()
        if index >= 0:
            return self._list.GetItemText(index)
        return self._selected_emoji

    def _queue_current_emoji(self) -> bool:
        """Keep one emoji and leave the picker open for another choice."""
        emoji = self._current_emoji()
        if not emoji:
            wx.Bell()
            return False
        self._queued_emojis.append(emoji)
        message = self._i18n.t("emoji_picker_queued").format(
            emoji=emoji, count=len(self._queued_emojis)
        )
        self._result_status.SetLabel(message)
        if callable(self._announce):
            try:
                self._announce(message, interrupt=True)
            except TypeError:
                self._announce(message)
        return True

    def _on_char_hook(self, event):
        if (self._list.HasFocus()
                and event.GetKeyCode() in (wx.WXK_RETURN, wx.WXK_NUMPAD_ENTER)
                and event.ControlDown()):
            if self._reaction_mode:
                self._on_ok(event)
            else:
                self._queue_current_emoji()
            return
        if (self._list.HasFocus()
                and event.GetKeyCode() in (wx.WXK_LEFT, wx.WXK_RIGHT)):
            # Single-column list: horizontal arrows walk the skin tone ladder
            # of the highlighted emoji when it has one, instead of scrolling.
            if self._cycle_skin_tone(1 if event.GetKeyCode() == wx.WXK_RIGHT else -1):
                return
        event.Skip()

    def _cycle_skin_tone(self, delta: int) -> bool:
        """Swap the selected row through its tone variants; True when moved."""
        index = self._list.GetFirstSelected()
        if index < 0:
            return False
        current = self._list.GetItemText(index)
        variants = _family_of(current)
        if len(variants) < 2 or current not in variants:
            return False
        next_emoji = variants[(variants.index(current) + delta) % len(variants)]
        self._list.SetItem(index, 0, next_emoji)
        # Screen readers name an emoji (tone included) themselves, so
        # announcing just the character keeps the cue language-neutral.
        if callable(self._announce):
            try:
                self._announce(next_emoji, interrupt=True)
            except TypeError:
                self._announce(next_emoji)
        return True

    def _final_selection(self) -> str:
        """One reaction, or queued composer emojis followed by the last one."""
        current = self._current_emoji()
        if self._reaction_mode:
            return current
        return "".join(self._queued_emojis) + current

    def _on_activated(self, event):
        self._on_selected(event)
        selected = self._final_selection()
        if selected:
            self._selected_emoji = selected
            self.EndModal(wx.ID_OK)

    def _on_ok(self, event):
        selected = self._final_selection()
        if selected:
            self._selected_emoji = selected
            self.EndModal(wx.ID_OK)

    def get_selected_emoji(self) -> str:
        return self._selected_emoji


def choose_and_insert_emoji(parent, text_ctrl, i18n) -> bool:
    """Show the picker and insert the chosen emoji into ``text_ctrl``."""
    dialog = EmojiPickerDialog(parent, i18n)
    try:
        if dialog.ShowModal() != wx.ID_OK:
            text_ctrl.SetFocus()
            return False
        return insert_emoji(text_ctrl, dialog.get_selected_emoji())
    finally:
        dialog.Destroy()


def choose_reaction_emoji(parent, i18n) -> str | None:
    """Select exactly one reaction from the complete emoji catalogue."""
    dialog = EmojiPickerDialog(parent, i18n, reaction_mode=True)
    try:
        if dialog.ShowModal() == wx.ID_OK:
            return dialog.get_selected_emoji() or None
        return None
    finally:
        dialog.Destroy()
