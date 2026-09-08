import "./DiscreteSlider.css";

export default function DiscreteSlider({ stops, selectedIndex, onSelect, ariaLabel, markPrefix, valueText, children }) {
  const maximum = Math.max(1, stops.length - 1);
  return <div className="runtime-slider-row runtime-discrete-control">
    <div className="runtime-discrete-track">
      <input type="range" min="0" max={Math.max(0, stops.length - 1)} step="1"
        value={selectedIndex} aria-label={ariaLabel} aria-valuetext={valueText || stops[selectedIndex]?.label}
        style={{"--range-fill":(selectedIndex / maximum * 100) + "%"}}
        onChange={event => onSelect(stops[Number(event.target.value)])}/>
      <div className="runtime-slider-marks">
        {stops.map((stop,index) => <button type="button" key={String(stop.value) + ":" + index}
          className={index === selectedIndex ? "active" : ""}
          style={{left:"calc(var(--slider-thumb-size) / 2 + (100% - var(--slider-thumb-size)) * " + (index / maximum) + ")"}}
          aria-label={markPrefix + stop.label} aria-pressed={index === selectedIndex}
          onClick={() => onSelect(stop)}><i/><span>{stop.label}</span></button>)}
      </div>
    </div>
    {children}
  </div>;
}
