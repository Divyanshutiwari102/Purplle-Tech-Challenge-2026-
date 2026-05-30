import React, { useState } from "react";
import { cameraSrc } from "../api";

export const CameraFeed: React.FC<{ cameras: string[] }> = ({ cameras }) => {
  const [active, setActive] = useState(cameras[0] ?? "CAM_1");
  const list = cameras.length ? cameras : ["CAM_1", "CAM_2", "CAM_3", "CAM_4", "CAM_5"];

  return (
    <div className="bg-panel rounded-xl border border-line p-3 flex flex-col gap-2">
      <div className="flex items-center justify-between">
        <div className="text-sm font-medium">Camera feed (simulated YOLOv8n)</div>
        <div className="flex gap-1">
          {list.map((c) => (
            <button
              key={c}
              className={`px-2 py-1 rounded text-xs border ${
                active === c
                  ? "bg-accent text-black border-accent"
                  : "bg-panel2 text-zinc-300 border-line hover:border-accent/50"
              }`}
              onClick={() => setActive(c)}
            >
              {c}
            </button>
          ))}
        </div>
      </div>
      <div className="rounded-lg overflow-hidden border border-line bg-black aspect-video">
        <img
          key={active}
          src={cameraSrc(active)}
          alt={`live ${active}`}
          className="w-full h-full object-contain"
        />
      </div>
      <div className="text-xs text-zinc-500">
        Synthetic frames with bounding boxes, zone polygons and HUD. The CCTV
        clips themselves are challenge-licensed and never transmitted.
      </div>
    </div>
  );
};

export default CameraFeed;
