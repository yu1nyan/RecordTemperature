using System.Text.Json;

namespace RecordTemperatureWidget;

/// <summary>1台のカメラ（温湿度計）の最新の記録。</summary>
internal sealed record CameraReading(int Id, string Name, bool Enabled, double? Temperature, double? Humidity, long? Ts);

/// <summary>RecordTemperature の /api/status の取得結果。取得できなかったときは Error に理由が入る。</summary>
internal sealed record StatusSnapshot(IReadOnlyList<CameraReading> Cameras, int IntervalSec, string? Error, string BaseUrl);

/// <summary>RecordTemperature の Web API から最新の値を取ってくる。</summary>
internal static class StatusClient
{
    private const string DefaultBaseUrl = "http://127.0.0.1:8765";
    private static readonly HttpClient Http = new() { Timeout = TimeSpan.FromSeconds(5) };
    private static readonly SemaphoreSlim Gate = new(1, 1);
    private static StatusSnapshot? cache;
    private static DateTime cacheTime;

    /// <summary>
    /// 接続先。Python 側が起動時に %LOCALAPPDATA%\RecordTemperature\endpoint.json に書き出す。
    /// ファイルがなければ既定のポート 8765 を使う。
    /// </summary>
    public static string BaseUrl()
    {
        try
        {
            string path = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "RecordTemperature", "endpoint.json");
            if (File.Exists(path))
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(path));
                if (doc.RootElement.TryGetProperty("url", out var url) && url.GetString() is { Length: > 0 } s)
                {
                    return s.TrimEnd('/');
                }
            }
        }
        catch (Exception e) when (e is IOException or JsonException or UnauthorizedAccessException)
        {
            Log.Write($"endpoint.json を読めない: {e.Message}");
        }
        return DefaultBaseUrl;
    }

    /// <summary>最新の状態を取る。ウィジェットが複数あっても通信は10秒に1回までにする。</summary>
    public static async Task<StatusSnapshot> FetchAsync()
    {
        await Gate.WaitAsync();
        try
        {
            if (cache is not null && DateTime.UtcNow - cacheTime < TimeSpan.FromSeconds(10))
            {
                return cache;
            }
            cache = await FetchUncachedAsync();
            cacheTime = DateTime.UtcNow;
            return cache;
        }
        finally
        {
            Gate.Release();
        }
    }

    private static async Task<StatusSnapshot> FetchUncachedAsync()
    {
        string baseUrl = BaseUrl();
        try
        {
            using var stream = await Http.GetStreamAsync($"{baseUrl}/api/status");
            using var doc = await JsonDocument.ParseAsync(stream);
            var root = doc.RootElement;
            var cameras = new List<CameraReading>();
            foreach (var item in root.GetProperty("cameras").EnumerateArray())
            {
                var cam = item.GetProperty("camera");
                var good = item.GetProperty("last_good");
                cameras.Add(new CameraReading(
                    cam.GetProperty("id").GetInt32(),
                    cam.GetProperty("name").GetString() ?? "",
                    cam.GetProperty("enabled").ValueKind == JsonValueKind.True
                        || (cam.GetProperty("enabled").ValueKind == JsonValueKind.Number && cam.GetProperty("enabled").GetInt32() != 0),
                    Number(good, "temperature"),
                    Number(good, "humidity"),
                    good.ValueKind == JsonValueKind.Object && good.GetProperty("ts").ValueKind == JsonValueKind.Number
                        ? good.GetProperty("ts").GetInt64() : null));
            }
            int interval = root.TryGetProperty("interval", out var iv) && iv.ValueKind == JsonValueKind.Number ? iv.GetInt32() : 60;
            return new StatusSnapshot(cameras, interval, null, baseUrl);
        }
        catch (Exception e) when (e is HttpRequestException or TaskCanceledException or JsonException
                                      or KeyNotFoundException or InvalidOperationException)
        {
            Log.Write($"取得失敗 ({baseUrl}): {e.Message}");
            return new StatusSnapshot([], 60, "RecordTemperature に接続できません", baseUrl);
        }
    }

    private static double? Number(JsonElement obj, string name) =>
        obj.ValueKind == JsonValueKind.Object && obj.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.Number
            ? v.GetDouble() : null;
}

/// <summary>絶対湿度と、インフルエンザの目安（ダッシュボードと同じ基準）。</summary>
internal static class Climate
{
    /// <summary>絶対湿度 [g/m³]（Tetens の式による飽和水蒸気圧から計算）</summary>
    public static double AbsoluteHumidity(double tempC, double rh)
    {
        double es = 6.1078 * Math.Pow(10, 7.5 * tempC / (tempC + 237.3));
        return 216.7 * (rh / 100 * es) / (tempC + 273.15);
    }

    /// <summary>7 g/m³ 未満=警戒、11 g/m³ 未満=注意。色は Adaptive Cards の TextBlock.color の値。</summary>
    public static (string Label, string ShortLabel, string Color) FluLevel(double ah) => ah switch
    {
        < 7 => ("インフルエンザ 警戒", "警戒", "attention"),
        < 11 => ("インフルエンザ 注意", "注意", "warning"),
        _ => ("インフルエンザ 低リスク", "低リスク", "good"),
    };
}
