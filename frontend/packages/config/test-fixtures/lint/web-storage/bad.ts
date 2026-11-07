export function keep(value: string) {
  localStorage.setItem("x", value);
  window.sessionStorage.clear();
  return document.cookie;
}
