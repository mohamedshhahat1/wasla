"use client";

/**
 * The last resort when the root layout itself fails: no provider is available, so the page is
 * static and bilingual, with no detail at all.
 */
export default function GlobalError() {
  return (
    <html lang="en" dir="ltr">
      <body>
        <main>
          <h1>Something went wrong</h1>
          <p lang="ar" dir="rtl">
            حدث خطأ ما
          </p>
          <p>
            <a href="/">Wasla</a>
          </p>
        </main>
      </body>
    </html>
  );
}
