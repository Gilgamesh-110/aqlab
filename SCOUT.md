round40 | local | phase E audit: lint translation rule vs placeholder-only values | 4 false positives removed on the reference APK | used
round41 | https://gitlab.pyicu.org/main/pyicu | ICU C++ API binding: plural/ordinal selectors, locale canonicalisation, script dir | we hand-roll CLDR plural counts; ICU is authoritative | pending
round41 | https://github.com/nidhaloff/deep_translator | free MT wrappers (Google/Libre/MyMemory) for filling values-ar/ | could seed untranslated strings instead of leaving them empty | pending
round41 | https://pypi.org/project/lingua-py/ | Rust language detector, offline, accurate on short strings | would let lint flag wrong-locale text (Latin inside values-ar) without a whitelist | pending
round41 | https://github.com/LonamiWebs/Stringlate | GUI strings.xml translator, 105 stars, dead since 2019 | GUI only, no CLI/schema, nothing to reuse | no
round41 | https://developer.android.com/about/versions/16/features | Android 16 i18n: VERTICAL_TEXT_FLAG, per-app locale APIs | no resource-level RTL change we can lint | no
round41 | note | gh search --topic android returns noise; topic i18n/rtl empty | narrow to repo names, not topics | used
