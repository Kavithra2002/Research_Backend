/**
 * Decode Google News RSS wrapper URLs to the publisher article URL.
 * Adapted from https://gist.github.com/huksley/bc3cb046157a99cd9d1517b32f91a99e (MIT).
 */

function fetchDecodedBatchExecute(id: string): Promise<string> {
  const s =
    '[[["Fbv4je","[\\"garturlreq\\",[[\\"en-US\\",\\"US\\",[\\"FINANCE_TOP_INDICES\\",\\"WEB_TEST_1_0_0\\"],null,null,1,1,\\"US:en\\",null,180,null,null,null,null,null,0,null,null,[1608992183,723341000]],\\"en-US\\",\\"US\\",1,[2,3,4,8],1,0,\\"655000234\\",0,0,null,0],\\"' +
    id +
    '\\"]",null,"generic"]]]';

  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10_000);

  return fetch(
    "https://news.google.com/_/DotsSplashUi/data/batchexecute?rpcids=Fbv4je",
    {
      method: "POST",
      signal: controller.signal,
      headers: {
        "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        Referer: "https://news.google.com/",
        "User-Agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
      },
      body: "f.req=" + encodeURIComponent(s),
    },
  )
    .then((res) => res.text())
    .then((text) => {
      const header = '[\\"garturlres\\",\\"';
      const footer = '\\",';
      if (!text.includes(header)) {
        throw new Error("garturlres header not found");
      }
      const start = text.substring(text.indexOf(header) + header.length);
      if (!start.includes(footer)) {
        throw new Error("garturlres footer not found");
      }
      return start.substring(0, start.indexOf(footer));
    })
    .finally(() => clearTimeout(timer));
}

export async function decodeGoogleNewsUrl(sourceUrl: string): Promise<string> {
  let url: URL;
  try {
    url = new URL(sourceUrl);
  } catch {
    return sourceUrl;
  }

  const path = url.pathname.split("/");
  if (
    url.hostname !== "news.google.com" ||
    path.length < 2 ||
    path[path.length - 2] !== "articles"
  ) {
    return sourceUrl;
  }

  const base64 = path[path.length - 1].split("?")[0];
  if (!base64) return sourceUrl;

  try {
    let str = Buffer.from(base64, "base64").toString("binary");

    const prefix = Buffer.from([0x08, 0x13, 0x22]).toString("binary");
    if (str.startsWith(prefix)) str = str.substring(prefix.length);

    const suffix = Buffer.from([0xd2, 0x01, 0x00]).toString("binary");
    if (str.endsWith(suffix)) str = str.substring(0, str.length - suffix.length);

    const bytes = Uint8Array.from(str, (c) => c.charCodeAt(0));
    const len = bytes[0] ?? 0;
    if (len >= 0x80) {
      str = str.substring(2, len + 2);
    } else {
      str = str.substring(1, len + 1);
    }

    if (str.startsWith("http://") || str.startsWith("https://")) {
      return str;
    }

    return await fetchDecodedBatchExecute(base64);
  } catch {
    try {
      return await fetchDecodedBatchExecute(base64);
    } catch {
      return sourceUrl;
    }
  }
}

export function isGoogleNewsUrl(url: string): boolean {
  try {
    const u = new URL(url);
    return (
      u.hostname === "news.google.com" &&
      u.pathname.includes("/articles/")
    );
  } catch {
    return false;
  }
}
