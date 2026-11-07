/** Sample text for stories: long Arabic, Turkish with İ/ı, and mixed direction (DS-05). */

export const LONG_ARABIC =
  "مرحبًا بك في وصلة، خدمة العملاء والمبيعات متعددة القنوات المدعومة بالذكاء الاصطناعي لكل نشاط تجاري";
export const LONG_TURKISH =
  "İstanbul'daki ıssız ışıklı iş yerimizde İĞNE ve ıhlamur ile ilgili ılık bir İçerik";
export const MIXED_NAME = "Ahmed أحمد #1024";

/** Short labels per locale, so a story reads naturally in either direction. */
export const LABELS = {
  en: { save: "Save", cancel: "Cancel", close: "Close", open: "Open", more: "More options" },
  ar: { save: "حفظ", cancel: "إلغاء", close: "إغلاق", open: "فتح", more: "خيارات أخرى" },
} as const;
