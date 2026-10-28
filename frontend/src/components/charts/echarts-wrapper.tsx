import { useRef, useEffect, useState } from "react";
import * as echarts from "echarts/core";
import { BarChart, CandlestickChart, LineChart, PieChart } from "echarts/charts";
import {
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent,
  DatasetComponent,
  DataZoomComponent,
} from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";

echarts.use([
  BarChart,
  CandlestickChart,
  LineChart,
  PieChart,
  TitleComponent,
  TooltipComponent,
  LegendComponent,
  GridComponent,
  DatasetComponent,
  DataZoomComponent,
  CanvasRenderer,
]);

const DARK_THEME: echarts.EChartsCoreOption = {
  backgroundColor: "transparent",
  textStyle: { color: "#e2e8f0" },
  legend: { textStyle: { color: "#cbd5e1" } },
  tooltip: {
    backgroundColor: "#1e293b",
    borderColor: "#334155",
    textStyle: { color: "#e2e8f0" },
  },
  xAxis: { axisLine: { lineStyle: { color: "#475569" } }, splitLine: { lineStyle: { color: "#334155" } } },
  yAxis: { axisLine: { lineStyle: { color: "#475569" } }, splitLine: { lineStyle: { color: "#334155" } } },
};

const LIGHT_THEME: echarts.EChartsCoreOption = {
  backgroundColor: "transparent",
  textStyle: { color: "#1e293b" },
  legend: { textStyle: { color: "#475569" } },
  tooltip: {
    backgroundColor: "#ffffff",
    borderColor: "#e2e8f0",
    textStyle: { color: "#1e293b" },
  },
  xAxis: { axisLine: { lineStyle: { color: "#cbd5e1" } }, splitLine: { lineStyle: { color: "#f1f5f9" } } },
  yAxis: { axisLine: { lineStyle: { color: "#cbd5e1" } }, splitLine: { lineStyle: { color: "#f1f5f9" } } },
};

function useIsDark(): boolean {
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"));

  useEffect(() => {
    const observer = new MutationObserver(() => {
      setDark(document.documentElement.classList.contains("dark"));
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => observer.disconnect();
  }, []);

  return dark;
}

interface Props {
  option: echarts.EChartsCoreOption;
  className?: string;
  style?: React.CSSProperties;
}

export function EChartsWrapper({ option, className, style }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<echarts.ECharts | null>(null);
  const isDark = useIsDark();

  useEffect(() => {
    if (!containerRef.current) return;
    chartRef.current = echarts.init(containerRef.current);

    const ro = new ResizeObserver(() => chartRef.current?.resize());
    ro.observe(containerRef.current);

    return () => {
      ro.disconnect();
      chartRef.current?.dispose();
    };
  }, []);

  useEffect(() => {
    if (!chartRef.current) return;
    chartRef.current.setOption(option, true);
    chartRef.current.setOption(isDark ? DARK_THEME : LIGHT_THEME, false);
  }, [option, isDark]);

  return (
    <div
      ref={containerRef}
      className={className}
      style={{ width: "100%", height: 300, ...style }}
    />
  );
}
