import { decodeGoogleNewsUrl } from "../app/services/googleNewsUrlDecoder";

async function main() {
  const gn =
    "https://news.google.com/rss/articles/CBMie0FVX3lxTE9XRTRQVTU1UEpUaEs3SDJWXzRvZ09nc2Q4MGw4RGhvV3UtSm5FN3ZYMzFhQzdNYjJTU1Uydmp6QUhtQjlkdU9MRlFINGJTVWJ4cFB4X09XQVB0d0ZzRW5La1k1ejBaRHFySG9xb0ZBelFOMjZ1bkdiYWRWUQ?oc=5";
  const decoded = await decodeGoogleNewsUrl(gn);
  console.log("decoded:", decoded);
}

void main();
