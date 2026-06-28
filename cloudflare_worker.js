// YouTube Transcript Cloudflare Worker
// Deploy at: https://dash.cloudflare.com → Workers & Pages → Create Worker
// Then add WORKER_URL = https://your-worker.workers.dev to Railway Variables

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const videoId = url.searchParams.get("videoId");

    if (!videoId) {
      return Response.json({ error: "videoId param required" }, { status: 400 });
    }

    try {
      // Call YouTube's internal InnerTube API (works from Cloudflare IPs)
      const playerRes = await fetch("https://www.youtube.com/youtubei/v1/player", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "User-Agent": "com.google.android.youtube/17.31.35 (Linux; U; Android 11) gzip",
          "X-YouTube-Client-Name": "3",
          "X-YouTube-Client-Version": "17.31.35",
        },
        body: JSON.stringify({
          videoId,
          context: {
            client: {
              clientName: "ANDROID",
              clientVersion: "17.31.35",
              androidSdkVersion: 30,
              hl: "en",
              gl: "US",
            },
          },
        }),
      });

      const playerData = await playerRes.json();
      const tracks = playerData?.captions?.playerCaptionsTracklistRenderer?.captionTracks;

      if (!tracks?.length) {
        return Response.json(
          { error: "No captions available for this video." },
          { status: 404 }
        );
      }

      // Prefer: manual English > auto English > first available
      const track =
        tracks.find((t) => t.languageCode === "en" && !t.kind) ||
        tracks.find((t) => t.languageCode === "en") ||
        tracks[0];

      const lang = track.languageCode || "en";

      // Fetch the actual transcript in json3 format
      const captionRes = await fetch(track.baseUrl + "&fmt=json3");
      const captionData = await captionRes.json();

      // Parse json3 into plain text
      const texts = [];
      for (const event of captionData.events || []) {
        for (const seg of event.segs || []) {
          const t = (seg.utf8 || "").trim();
          if (t && t !== "\n") texts.push(t);
        }
      }

      const transcript = texts.join(" ").replace(/\s+/g, " ").trim();

      if (!transcript) {
        return Response.json({ error: "Transcript was empty." }, { status: 404 });
      }

      return Response.json(
        { transcript, lang },
        { headers: { "Access-Control-Allow-Origin": "*" } }
      );
    } catch (e) {
      return Response.json({ error: e.message }, { status: 500 });
    }
  },
};
