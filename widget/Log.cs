namespace RecordTemperatureWidget;

/// <summary>
/// 動作ログ。ウィジェット提供アプリには画面がないので、問題の調査用にファイルへ書く。
/// 場所: %LOCALAPPDATA%\Packages\(パッケージ名)\LocalState\widget.log
/// </summary>
internal static class Log
{
    private static readonly object Gate = new();
    private static string? path;

    public static void Write(string message)
    {
        try
        {
            lock (Gate)
            {
                path ??= Path.Combine(Windows.Storage.ApplicationData.Current.LocalFolder.Path, "widget.log");
                var info = new FileInfo(path);
                if (info.Exists && info.Length > 1_000_000)
                {
                    File.Move(path, path + ".1", overwrite: true);
                }
                File.AppendAllText(path, $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} [{Environment.CurrentManagedThreadId}] {message}{Environment.NewLine}");
            }
        }
        catch
        {
            // ログが書けなくてもウィジェットの動作は続ける（パッケージ外で起動した場合など）
        }
    }
}
