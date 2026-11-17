using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Windows.Widgets;
using Microsoft.Windows.Widgets.Providers;

namespace RecordTemperatureWidget;

/// <summary>
/// ウィジェットホストから呼ばれる本体。
/// ウィジェットごとに表示するカメラ（CustomState に保存）を持ち、表示中は1分ごとに値を更新する。
/// カメラはウィジェットの「…」→「ウィジェットのカスタマイズ」で選ぶ。
/// </summary>
[ComVisible(true)]
[ComDefaultInterface(typeof(IWidgetProvider))]
[Guid("EB477AAF-E3B8-400E-A75E-22FE72B15225")]
internal sealed partial class WidgetProvider : IWidgetProvider, IWidgetProvider2
{
    private sealed class WidgetState(string id, WidgetSize size, int? cameraId)
    {
        public string Id { get; } = id;
        public WidgetSize Size { get; set; } = size;
        public int? CameraId { get; set; } = cameraId;
        public bool Active { get; set; }
        public bool Customizing { get; set; }
    }

    public static readonly ManualResetEvent EmptyWidgetListEvent = new(false);

    private static readonly Dictionary<string, WidgetState> Widgets = [];
    private static readonly object Gate = new();
    private static readonly Dictionary<string, string> Templates = [];
    private static Timer? timer;

    public WidgetProvider()
    {
        // プロセスが再起動したときは、すでに置かれているウィジェットを引き継ぐ
        WidgetInfo[] infos;
        try
        {
            infos = WidgetManager.GetDefault().GetWidgetInfos() ?? [];
        }
        catch (Exception e)
        {
            Log.Write($"既存ウィジェットの取得に失敗: {e}");
            infos = [];
        }
        foreach (var info in infos)
        {
            var ctx = info.WidgetContext;
            lock (Gate)
            {
                Widgets.TryAdd(ctx.Id, new WidgetState(ctx.Id, ctx.Size, ParseCameraId(info.CustomState))
                {
                    Active = ctx.IsActive,
                });
            }
        }
        Log.Write($"WidgetProvider 作成（既存ウィジェット {Widgets.Count} 件）");
        // COM から複数回作られることがあるので、タイマーはプロセスで1つだけにする
        timer ??= new Timer(_ => UpdateActiveWidgets(), null, TimeSpan.Zero, TimeSpan.FromSeconds(60));
    }

    // ------------------------------------------------------------------ IWidgetProvider

    public void CreateWidget(WidgetContext widgetContext)
    {
        Log.Write($"CreateWidget {widgetContext.Id} {widgetContext.Size}");
        var state = new WidgetState(widgetContext.Id, widgetContext.Size, null) { Active = true };
        lock (Gate)
        {
            Widgets[state.Id] = state;
        }
        Refresh(state);
    }

    public void DeleteWidget(string widgetId, string customState)
    {
        Log.Write($"DeleteWidget {widgetId}");
        lock (Gate)
        {
            Widgets.Remove(widgetId);
            if (Widgets.Count == 0)
            {
                EmptyWidgetListEvent.Set();
            }
        }
    }

    public void OnActionInvoked(WidgetActionInvokedArgs actionInvokedArgs)
    {
        var state = Find(actionInvokedArgs.WidgetContext.Id);
        if (state is null)
        {
            return;
        }
        Log.Write($"OnActionInvoked {state.Id} {actionInvokedArgs.Verb} {actionInvokedArgs.Data}");
        switch (actionInvokedArgs.Verb)
        {
            case "save":
                state.CameraId = ParseCameraIdFromInputs(actionInvokedArgs.Data) ?? state.CameraId;
                state.Customizing = false;
                break;
            case "cancel":
                state.Customizing = false;
                break;
            case "open":
                OpenDashboard();
                return;
        }
        Refresh(state);
    }

    public void OnWidgetContextChanged(WidgetContextChangedArgs contextChangedArgs)
    {
        var ctx = contextChangedArgs.WidgetContext;
        var state = Find(ctx.Id);
        if (state is null)
        {
            return;
        }
        state.Size = ctx.Size;
        Refresh(state);
    }

    public void Activate(WidgetContext widgetContext)
    {
        var state = Find(widgetContext.Id);
        if (state is null)
        {
            return;
        }
        state.Active = true;
        Refresh(state);
    }

    public void Deactivate(string widgetId)
    {
        var state = Find(widgetId);
        if (state is not null)
        {
            state.Active = false;
        }
    }

    // ------------------------------------------------------------------ IWidgetProvider2

    public void OnCustomizationRequested(WidgetCustomizationRequestedArgs customizationRequestedArgs)
    {
        var state = Find(customizationRequestedArgs.WidgetContext.Id);
        if (state is null)
        {
            return;
        }
        state.Customizing = true;
        Refresh(state);
    }

    // ------------------------------------------------------------------ 表示の更新

    private static WidgetState? Find(string id)
    {
        lock (Gate)
        {
            return Widgets.GetValueOrDefault(id);
        }
    }

    private static void UpdateActiveWidgets()
    {
        List<WidgetState> active;
        lock (Gate)
        {
            active = Widgets.Values.Where(w => w.Active && !w.Customizing).ToList();
        }
        foreach (var state in active)
        {
            Refresh(state);
        }
    }

    private static void Refresh(WidgetState state) => _ = RefreshAsync(state);

    private static async Task RefreshAsync(WidgetState state)
    {
        try
        {
            var snapshot = await StatusClient.FetchAsync();
            var (template, data) = state.Customizing ? BuildCustomize(state, snapshot) : BuildReading(state, snapshot);
            var options = new WidgetUpdateRequestOptions(state.Id)
            {
                Template = template,
                Data = data.ToJsonString(),
                CustomState = state.CameraId?.ToString() ?? "",
            };
            WidgetManager.GetDefault().UpdateWidget(options);
        }
        catch (Exception e)
        {
            Log.Write($"更新失敗 {state.Id}: {e}");
        }
    }

    /// <summary>温湿度の表示。カメラ未選択なら有効なカメラの先頭を表示する。</summary>
    private static (string, JsonObject) BuildReading(WidgetState state, StatusSnapshot snapshot)
    {
        string template = LoadTemplate(state.Size == WidgetSize.Small ? "reading_small.json" : "reading_medium.json");
        var cam = snapshot.Cameras.FirstOrDefault(c => c.Id == state.CameraId)
                  ?? snapshot.Cameras.FirstOrDefault(c => c.Enabled)
                  ?? snapshot.Cameras.FirstOrDefault();

        if (snapshot.Error is not null || cam is null)
        {
            return (template, new JsonObject
            {
                ["name"] = "温湿度",
                ["temp"] = "--",
                ["hum"] = "--",
                ["abs"] = "--",
                ["flu"] = snapshot.Error ?? "カメラが登録されていません",
                ["fluShort"] = snapshot.Error is null ? "カメラ未登録" : "接続できません",
                ["fluColor"] = "attention",
                ["updated"] = snapshot.Error is null ? "" : "アプリが起動しているか確認してください",
            });
        }

        string abs = "--", flu = "記録がありません", fluShort = "記録なし", color = "default";
        if (cam.Temperature is double t && cam.Humidity is double h)
        {
            double ah = Climate.AbsoluteHumidity(t, h);
            (flu, fluShort, color) = Climate.FluLevel(ah);
            abs = $"{ah:0.0} g/m³";
        }

        string updated = "";
        if (cam.Ts is long ts)
        {
            var time = DateTimeOffset.FromUnixTimeSeconds(ts).ToLocalTime();
            bool stale = DateTimeOffset.Now - time > TimeSpan.FromSeconds(snapshot.IntervalSec * 5);
            updated = $"{time:HH:mm} 時点" + (stale ? "（更新が止まっています）" : "");
        }

        return (template, new JsonObject
        {
            ["name"] = cam.Name,
            ["temp"] = cam.Temperature is double tv ? $"{tv:0.0}℃" : "--",
            ["hum"] = cam.Humidity is double hv ? $"{hv:0}%" : "--",
            ["abs"] = abs,
            ["flu"] = flu,
            ["fluShort"] = fluShort,
            ["fluColor"] = color,
            ["updated"] = updated,
        });
    }

    /// <summary>カスタマイズ画面（表示するカメラの選択）。</summary>
    private static (string, JsonObject) BuildCustomize(WidgetState state, StatusSnapshot snapshot)
    {
        var choices = new JsonArray();
        foreach (var c in snapshot.Cameras)
        {
            choices.Add(new JsonObject { ["title"] = c.Name, ["value"] = c.Id.ToString() });
        }
        int? selected = state.CameraId ?? snapshot.Cameras.FirstOrDefault(c => c.Enabled)?.Id;
        string note = snapshot.Error is not null ? $"{snapshot.Error}。アプリを起動してから、もう一度開いてください。"
            : snapshot.Cameras.Count == 0 ? "カメラが登録されていません。" : "";
        return (LoadTemplate("customize.json"), new JsonObject
        {
            ["choices"] = choices,
            ["selected"] = selected?.ToString() ?? "",
            ["note"] = note,
        });
    }

    /// <summary>確認用（Program.cs の -SelfTest）。各表示のテンプレート名とデータを返す。</summary>
    internal static async Task<string> SelfTestAsync()
    {
        var snapshot = await StatusClient.FetchAsync();
        var lines = new List<string> { $"接続先: {snapshot.BaseUrl} / エラー: {snapshot.Error ?? "なし"}" };
        foreach (var (label, state) in new[]
                 {
                     ("small", new WidgetState("t", WidgetSize.Small, null)),
                     ("medium", new WidgetState("t", WidgetSize.Medium, null)),
                     ("customize", new WidgetState("t", WidgetSize.Medium, null) { Customizing = true }),
                 })
        {
            var (template, data) = state.Customizing ? BuildCustomize(state, snapshot) : BuildReading(state, snapshot);
            JsonNode.Parse(template);   // テンプレートが正しい JSON か確認
            lines.Add($"[{label}] {data.ToJsonString(new JsonSerializerOptions { Encoder = System.Text.Encodings.Web.JavaScriptEncoder.UnsafeRelaxedJsonEscaping })}");
        }
        return string.Join(Environment.NewLine, lines) + Environment.NewLine;
    }

    private static string LoadTemplate(string name)
    {
        lock (Templates)
        {
            if (!Templates.TryGetValue(name, out var text))
            {
                using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(name)
                                   ?? throw new InvalidOperationException($"テンプレートがない: {name}");
                using var reader = new StreamReader(stream);
                text = reader.ReadToEnd();
                Templates[name] = text;
            }
            return text;
        }
    }

    private static int? ParseCameraId(string? text) => int.TryParse(text, out int id) ? id : null;

    /// <summary>カスタマイズ画面の入力（{"cameraId":"2"}）からカメラ ID を取り出す。</summary>
    private static int? ParseCameraIdFromInputs(string? json)
    {
        if (string.IsNullOrEmpty(json))
        {
            return null;
        }
        try
        {
            using var doc = JsonDocument.Parse(json);
            return doc.RootElement.TryGetProperty("cameraId", out var v) ? ParseCameraId(v.ToString()) : null;
        }
        catch (JsonException)
        {
            return null;
        }
    }

    private static void OpenDashboard()
    {
        try
        {
            Process.Start(new ProcessStartInfo(StatusClient.BaseUrl() + "/") { UseShellExecute = true });
        }
        catch (Exception e)
        {
            Log.Write($"ブラウザを開けない: {e.Message}");
        }
    }
}
