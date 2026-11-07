/**
 * The workspace's own lint rules (WSP-03, WSP-04 of FE1_FOUNDATION.md). Each rule has a
 * fixture that must fail and a clean twin that must pass (packages/config/test).
 */

const VARIANT_PREFIX = /^(?:[a-z0-9-]+:|\[[^\]]+\]:)*/i;

/** Splits a string into class-like tokens without variant prefixes, `!` or a leading `-`. */
function classTokens(text) {
  return text
    .split(/\s+/)
    .filter(Boolean)
    .map((token) => token.replace(VARIANT_PREFIX, "").replace(/^!/, "").replace(/^-/, ""));
}

/** Visits every string literal and template chunk that is not an import source or a key. */
function eachStringChunk(context, visit) {
  const inspect = (node, text) => {
    const parent = node.parent;
    if (!parent) return;
    if (
      parent.type === "ImportDeclaration" ||
      parent.type === "ExportNamedDeclaration" ||
      parent.type === "ExportAllDeclaration" ||
      parent.type === "ImportExpression"
    ) {
      return;
    }
    if (parent.type === "Property" && parent.key === node) return;
    if (parent.type === "TSLiteralType" || parent.type === "TSExternalModuleReference") return;
    if (parent.type === "ExpressionStatement" && parent.directive) return;
    visit(node, text);
  };
  return {
    Literal(node) {
      if (typeof node.value === "string") inspect(node, node.value);
    },
    TemplateElement(node) {
      inspect(node.parent, node.value.cooked ?? node.value.raw);
    },
  };
}

/** Yields [keyName, valueNode] for every property of a JSX `style={{...}}` object. */
function styleProperties(node) {
  if (node.name?.name !== "style") return [];
  const expression = node.value?.expression;
  if (!expression || expression.type !== "ObjectExpression") return [];
  return expression.properties
    .filter((p) => p.type === "Property")
    .map((p) => [p.key.type === "Identifier" ? p.key.name : String(p.key.value), p.value]);
}

const PHYSICAL_PREFIX =
  /^(?:ml|mr|pl|pr|scroll-ml|scroll-mr|scroll-pl|scroll-pr|left|right|border-l|border-r|rounded-l|rounded-r|rounded-tl|rounded-tr|rounded-bl|rounded-br)-/;
const PHYSICAL_EXACT =
  /^(?:border-l|border-r|rounded-l|rounded-r|rounded-tl|rounded-tr|rounded-bl|rounded-br|text-left|text-right|float-left|float-right|clear-left|clear-right)$/;
const PHYSICAL_STYLE_KEYS = new Set([
  "marginLeft",
  "marginRight",
  "paddingLeft",
  "paddingRight",
  "left",
  "right",
  "borderLeft",
  "borderRight",
  "borderLeftWidth",
  "borderRightWidth",
  "borderLeftColor",
  "borderRightColor",
  "borderTopLeftRadius",
  "borderTopRightRadius",
  "borderBottomLeftRadius",
  "borderBottomRightRadius",
]);

const noPhysicalDirection = {
  meta: {
    type: "problem",
    docs: { description: "Logical properties only: no physical left/right classes or styles." },
    messages: {
      cls: "Physical direction class '{{token}}': use the logical equivalent (ms-/me-/ps-/pe-/start-/end-/text-start/text-end/rounded-s/rounded-e/border-s/border-e).",
      style:
        "Physical direction style '{{key}}': use the logical property (marginInlineStart, insetInlineEnd, ...).",
      align: "textAlign '{{value}}': use 'start' or 'end'.",
    },
    schema: [],
  },
  create(context) {
    return {
      ...eachStringChunk(context, (node, text) => {
        for (const token of classTokens(text)) {
          if (PHYSICAL_PREFIX.test(token) || PHYSICAL_EXACT.test(token)) {
            context.report({ node, messageId: "cls", data: { token } });
          }
        }
      }),
      JSXAttribute(node) {
        for (const [key, value] of styleProperties(node)) {
          if (PHYSICAL_STYLE_KEYS.has(key)) {
            context.report({ node: value, messageId: "style", data: { key } });
          }
          if (
            key === "textAlign" &&
            value.type === "Literal" &&
            (value.value === "left" || value.value === "right")
          ) {
            context.report({ node: value, messageId: "align", data: { value: value.value } });
          }
        }
      },
    };
  },
};

const noUppercase = {
  meta: {
    type: "problem",
    docs: {
      description:
        "No CSS upper-casing and no locale-free case mapping (Turkish İ/ı, plan section 20).",
    },
    messages: {
      cls: "The 'uppercase' class breaks Turkish casing; write the text in the case it should show.",
      style: "textTransform 'uppercase' breaks Turkish casing.",
      call: "{{name}}() ignores the locale: use toLocale{{suffix}}(locale).",
    },
    schema: [],
  },
  create(context) {
    return {
      ...eachStringChunk(context, (node, text) => {
        if (classTokens(text).includes("uppercase")) context.report({ node, messageId: "cls" });
      }),
      JSXAttribute(node) {
        for (const [key, value] of styleProperties(node)) {
          if (key === "textTransform" && value.type === "Literal" && value.value === "uppercase") {
            context.report({ node: value, messageId: "style" });
          }
        }
      },
      CallExpression(node) {
        const callee = node.callee;
        if (callee.type !== "MemberExpression" || callee.property.type !== "Identifier") return;
        const name = callee.property.name;
        if (name === "toUpperCase" || name === "toLowerCase") {
          context.report({
            node,
            messageId: "call",
            data: { name, suffix: name === "toUpperCase" ? "UpperCase" : "LowerCase" },
          });
        }
      },
    };
  },
};

const SECRET_NAME = /^(?:NEXT_PUBLIC_|PUBLIC_).*(?:SECRET|KEY|TOKEN|PASSWORD)/;

const noPublicSecretEnv = {
  meta: {
    type: "problem",
    docs: { description: "No secret-looking name under NEXT_PUBLIC_ / PUBLIC_ (plan section 22)." },
    messages: {
      secret:
        "'{{name}}' is shipped to every browser: a name with SECRET/KEY/TOKEN/PASSWORD must not be public.",
    },
    schema: [
      {
        type: "object",
        properties: { allow: { type: "array", items: { type: "string" } } },
        additionalProperties: false,
      },
    ],
  },
  create(context) {
    const allow = new Set(context.options[0]?.allow ?? []);
    const check = (node, name) => {
      if (SECRET_NAME.test(name) && !allow.has(name)) {
        context.report({ node, messageId: "secret", data: { name } });
      }
    };
    return {
      MemberExpression(node) {
        const object = node.object;
        const isProcessEnv =
          object.type === "MemberExpression" &&
          object.object.type === "Identifier" &&
          object.object.name === "process" &&
          object.property.type === "Identifier" &&
          object.property.name === "env";
        const isImportMetaEnv =
          object.type === "MemberExpression" &&
          object.object.type === "MetaProperty" &&
          object.property.type === "Identifier" &&
          object.property.name === "env";
        if (!isProcessEnv && !isImportMetaEnv) return;
        if (node.property.type === "Identifier" && !node.computed) check(node, node.property.name);
        if (node.property.type === "Literal" && typeof node.property.value === "string") {
          check(node, node.property.value);
        }
      },
      ...eachStringChunk(context, (node, text) => {
        if (/^(?:NEXT_PUBLIC_|PUBLIC_)[A-Z0-9_]+$/.test(text)) check(node, text);
      }),
    };
  },
};

const ARBITRARY_VALUE = /\[[^\]]+\]/;
const HEX_COLOUR = /^#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
const RAW_STYLE_KEY =
  /color|background|fill|stroke|font|margin|padding|gap|radius|width|height|inset|top|bottom|shadow|lineHeight|letterSpacing|outline|border/i;
const RAW_VALUE = /(?:\d(?:px|rem|em|pt|vh|vw|%)|#[0-9a-f]{3,8}\b|rgba?\(|hsla?\(|oklch\()/i;

const noRawDesignValues = {
  meta: {
    type: "problem",
    docs: {
      description:
        "Screens use tokens and ui components only: no arbitrary values, colours or sizes (DS-01).",
    },
    messages: {
      arbitrary:
        "Arbitrary value '{{token}}': add a token to packages/ui/src/tokens.css or a component to packages/ui.",
      hex: "Raw colour '{{token}}': use a colour token.",
      style: "Raw design value in style '{{key}}': use a token or a ui component.",
    },
    schema: [],
  },
  create(context) {
    return {
      ...eachStringChunk(context, (node, text) => {
        for (const token of classTokens(text)) {
          if (/^[a-z][\w-]*-\[/.test(token) && ARBITRARY_VALUE.test(token)) {
            context.report({ node, messageId: "arbitrary", data: { token } });
          } else if (
            HEX_COLOUR.test(token) &&
            !(
              node.parent?.type === "JSXAttribute" &&
              /^(?:href|id|to)$/.test(node.parent.name?.name)
            )
          ) {
            context.report({ node, messageId: "hex", data: { token } });
          }
        }
      }),
      JSXAttribute(node) {
        for (const [key, value] of styleProperties(node)) {
          if (!RAW_STYLE_KEY.test(key)) continue;
          const raw =
            (value.type === "Literal" && typeof value.value === "number" && value.value !== 0) ||
            (value.type === "Literal" &&
              typeof value.value === "string" &&
              RAW_VALUE.test(value.value));
          if (raw) context.report({ node: value, messageId: "style", data: { key } });
        }
      },
    };
  },
};

const STORAGE_GLOBALS = new Set(["localStorage", "sessionStorage", "indexedDB", "cookieStore"]);

function normalised(filename) {
  return filename.replaceAll("\\", "/");
}

const noWebStorage = {
  meta: {
    type: "problem",
    docs: {
      description:
        "No web storage or script cookies outside the allow-listed session file (S-2, plan section 22).",
    },
    messages: {
      storage:
        "'{{name}}' is not used outside packages/session's allow-listed file: tokens never touch storage.",
      cookie: "document.cookie is not read or written by page script (S-2).",
      token: "The allow-listed storage file must not store a key containing 'token'.",
    },
    schema: [
      {
        type: "object",
        properties: { allowFiles: { type: "array", items: { type: "string" } } },
        additionalProperties: false,
      },
    ],
  },
  create(context) {
    const file = normalised(context.filename);
    const allowed = (context.options[0]?.allowFiles ?? []).some((suffix) => file.endsWith(suffix));
    return {
      Identifier(node) {
        if (allowed || !STORAGE_GLOBALS.has(node.name)) return;
        const parent = node.parent;
        if (parent?.type === "Property" && parent.key === node && !parent.computed) return;
        if (parent?.type === "MemberExpression" && parent.property === node && !parent.computed) {
          const owner = parent.object;
          if (!(
            owner.type === "Identifier" &&
            (owner.name === "window" || owner.name === "globalThis")
          )) {
            return;
          }
        }
        context.report({ node, messageId: "storage", data: { name: node.name } });
      },
      MemberExpression(node) {
        if (
          !allowed &&
          node.object.type === "Identifier" &&
          node.object.name === "document" &&
          node.property.type === "Identifier" &&
          node.property.name === "cookie"
        ) {
          context.report({ node, messageId: "cookie" });
        }
      },
      CallExpression(node) {
        if (!allowed) return;
        const callee = node.callee;
        if (callee.type !== "MemberExpression" || callee.property.type !== "Identifier") return;
        if (callee.property.name !== "setItem") return;
        const key = node.arguments[0];
        if (key?.type === "Literal" && typeof key.value === "string" && /token/i.test(key.value)) {
          context.report({ node: key, messageId: "token" });
        }
      },
    };
  },
};

const SERVER_ONLY_SOURCES = [
  /^@wasla\/session\/server(?:\/|$)/,
  /(?:^|\/)server\/forward(?:\.ts)?$/,
  /^node:crypto$/,
];

const noServerOnlyInClient = {
  meta: {
    type: "problem",
    docs: {
      description:
        "The forward signer and node:crypto stay on the server: never imported by client code (SS-03).",
    },
    messages: {
      server: "'{{source}}' is server-only and must not be imported here (a client bundle).",
    },
    schema: [
      {
        type: "object",
        properties: { clientFiles: { type: "array", items: { type: "string" } } },
        additionalProperties: false,
      },
    ],
  },
  create(context) {
    const file = normalised(context.filename);
    const clientByPath = (context.options[0]?.clientFiles ?? []).some((fragment) =>
      file.includes(fragment),
    );
    let isClient = clientByPath;
    const check = (node, source) => {
      if (isClient && SERVER_ONLY_SOURCES.some((pattern) => pattern.test(source))) {
        context.report({ node, messageId: "server", data: { source } });
      }
    };
    return {
      Program(node) {
        isClient =
          clientByPath ||
          node.body.some(
            (statement) =>
              statement.type === "ExpressionStatement" && statement.directive === "use client",
          );
      },
      ImportDeclaration(node) {
        check(node, String(node.source.value));
      },
      ImportExpression(node) {
        if (node.source.type === "Literal") check(node, String(node.source.value));
      },
    };
  },
};

const consolePlatformPaths = {
  meta: {
    type: "problem",
    docs: {
      description:
        "The console calls platform paths (and the sign-in paths staff use) only (plan section 17).",
    },
    messages: {
      path: "The console may call '/api/v1/platform/**' and '/api/v1/auth/**' only, not '{{path}}'.",
    },
    schema: [
      {
        type: "object",
        properties: { allow: { type: "array", items: { type: "string" } } },
        additionalProperties: false,
      },
    ],
  },
  create(context) {
    const allow = context.options[0]?.allow ?? ["/api/v1/platform/", "/api/v1/auth/"];
    return eachStringChunk(context, (node, text) => {
      if (text.startsWith("/api/v1/") && !allow.some((prefix) => text.startsWith(prefix))) {
        context.report({ node, messageId: "path", data: { path: text } });
      }
    });
  },
};

export default {
  meta: { name: "eslint-plugin-wasla", version: "0.0.0" },
  rules: {
    "no-physical-direction": noPhysicalDirection,
    "no-uppercase": noUppercase,
    "no-public-secret-env": noPublicSecretEnv,
    "no-raw-design-values": noRawDesignValues,
    "no-web-storage": noWebStorage,
    "no-server-only-in-client": noServerOnlyInClient,
    "console-platform-paths": consolePlatformPaths,
  },
};
