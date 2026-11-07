export const Title = ({ text, locale }: { text: string; locale: string }) => (
  <h1 className="font-semibold">{text.toLocaleUpperCase(locale)}</h1>
);
