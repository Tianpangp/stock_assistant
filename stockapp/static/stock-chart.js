(() => {
  const container = document.getElementById('stock-chart');
  const dataElement = document.getElementById('stock-chart-data');
  const predictionElement = document.getElementById('stock-prediction-data');
  const errorElement = document.querySelector('[data-chart-error]');
  if (!container || !dataElement) return;

  const showError = () => {
    if (errorElement) errorElement.hidden = false;
    container.textContent = '';
  };

  try {
    const bars = JSON.parse(dataElement.textContent || '[]');
    const predictedBars = JSON.parse(predictionElement?.textContent || '[]');
    if (!bars.length || !window.LightweightCharts) {
      showError();
      return;
    }

    const chart = LightweightCharts.createChart(container, {
      width: container.clientWidth,
      height: container.clientHeight,
      layout: {
        background: { type: 'solid', color: '#ffffff' },
        textColor: '#657168',
        fontFamily: 'system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
      },
      grid: {
        vertLines: { color: '#edf1ee' },
        horzLines: { color: '#edf1ee' },
      },
      rightPriceScale: { borderColor: '#dce3de' },
      timeScale: { borderColor: '#dce3de', timeVisible: false },
      crosshair: { mode: 0 },
    });

    const candles = chart.addCandlestickSeries({
      upColor: '#c83f37',
      downColor: '#17805b',
      borderUpColor: '#c83f37',
      borderDownColor: '#17805b',
      wickUpColor: '#c83f37',
      wickDownColor: '#17805b',
    });
    candles.setData(bars.map(({ time, open, high, low, close }) => ({
      time, open, high, low, close,
    })));
    candles.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.28 } });

    const volume = chart.addHistogramSeries({
      priceFormat: { type: 'volume' },
      priceScaleId: '',
      lastValueVisible: false,
      priceLineVisible: false,
    });
    volume.setData(bars.map((bar) => ({
      time: bar.time,
      value: bar.volume,
      color: bar.close >= bar.open ? 'rgba(200, 63, 55, .42)' : 'rgba(23, 128, 91, .42)',
    })));
    volume.priceScale().applyOptions({ scaleMargins: { top: 0.78, bottom: 0 } });

    let forecast = null;
    if (predictedBars.length) {
      forecast = chart.addCandlestickSeries({
        upColor: 'rgba(197, 138, 24, .55)',
        downColor: 'rgba(88, 119, 146, .55)',
        borderUpColor: '#a56d08',
        borderDownColor: '#46647e',
        wickUpColor: '#a56d08',
        wickDownColor: '#46647e',
        lastValueVisible: false,
        priceLineVisible: false,
      });
      forecast.setData(predictedBars.map(({ time, open, high, low, close }) => ({
        time, open, high, low, close,
      })));
      forecast.setMarkers([{
        time: predictedBars[0].time,
        position: 'aboveBar',
        color: '#95630d',
        shape: 'arrowDown',
        text: 'Kronos预测',
      }]);
    }

    const colors = {
      5: '#275d8c',
      10: '#95630d',
      20: '#176b4d',
      30: '#3f6c74',
      60: '#b33a32',
      120: '#6b5f8d',
    };
    const movingAverage = (period) => {
      let sum = 0;
      const values = [];
      bars.forEach((bar, index) => {
        sum += bar.close;
        if (index >= period) sum -= bars[index - period].close;
        if (index >= period - 1) values.push({ time: bar.time, value: sum / period });
      });
      return values;
    };

    const maSeries = new Map();
    document.querySelectorAll('.ma-toggle').forEach((input) => {
      const period = Number(input.value);
      const series = chart.addLineSeries({
        color: colors[period],
        lineWidth: 2,
        lastValueVisible: false,
        priceLineVisible: false,
        crosshairMarkerVisible: false,
        visible: input.checked,
      });
      series.setData(movingAverage(period));
      maSeries.set(period, series);
      input.addEventListener('change', () => series.applyOptions({ visible: input.checked }));
    });

    const legend = document.querySelector('[data-chart-legend]');
    const formatLegend = (bar, predicted = false) => {
      if (!bar || !legend) return;
      const change = bar.open ? (bar.close / bar.open - 1) * 100 : 0;
      const prefix = predicted ? 'Kronos预测 · ' : '';
      legend.textContent = `${prefix}${bar.time}  开 ${bar.open.toFixed(2)}  高 ${bar.high.toFixed(2)}  低 ${bar.low.toFixed(2)}  收 ${bar.close.toFixed(2)}  ${change >= 0 ? '+' : ''}${change.toFixed(2)}%`;
    };
    formatLegend(bars[bars.length - 1]);
    chart.subscribeCrosshairMove((param) => {
      const point = param.seriesData?.get(candles);
      const forecastPoint = forecast ? param.seriesData?.get(forecast) : null;
      if (forecastPoint && param.time) formatLegend({ ...forecastPoint, time: param.time }, true);
      else if (point && param.time) formatLegend({ ...point, time: param.time });
      else formatLegend(bars[bars.length - 1]);
    });

    const initialVisibleBars = Math.min(100, bars.length);
    chart.timeScale().setVisibleLogicalRange({
      from: bars.length - initialVisibleBars,
      to: bars.length + predictedBars.length - 1,
    });
    container.dataset.barCount = String(bars.length);
    container.dataset.predictionBarCount = String(predictedBars.length);
    container.dataset.initialVisibleBars = String(initialVisibleBars);
    const observer = new ResizeObserver(() => {
      chart.applyOptions({ width: container.clientWidth, height: container.clientHeight });
    });
    observer.observe(container);
  } catch (error) {
    showError();
  }
})();
