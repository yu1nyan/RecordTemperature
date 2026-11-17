using System.Runtime.InteropServices;
using Microsoft.Windows.Widgets.Providers;
using RecordTemperatureWidget;
using WinRT;

// ウィジェットホストは AppxManifest.xml の com:ExeServer の設定どおり
// "-RegisterProcessAsComServer" 付きでこの exe を起動し、WidgetProvider を COM 経由で作る。
Log.Write($"起動: {string.Join(' ', args)}");
ComWrappersSupport.InitializeComWrappers();

if (args.Length > 0 && args[0] == "-RegisterProcessAsComServer")
{
    int hr = Native.CoRegisterClassObject(
        typeof(WidgetProvider).GUID, new WidgetProviderFactory<WidgetProvider>(),
        Native.CLSCTX_LOCAL_SERVER, Native.REGCLS_MULTIPLEUSE, out uint cookie);
    if (hr != 0)
    {
        Log.Write($"CoRegisterClassObject 失敗: 0x{hr:X8}");
        return hr;
    }

    // ウィジェットがすべて削除されるまで待つ（削除されたらプロセスを終える）
    WidgetProvider.EmptyWidgetListEvent.WaitOne();
    Native.CoRevokeClassObject(cookie);
    Log.Write("終了: ウィジェットがなくなった");
}
else if (args.Length > 1 && args[0] == "-SelfTest")
{
    // 確認用: ウィジェットに渡すテンプレートとデータを、指定したファイルに書き出す
    File.WriteAllText(args[1], await WidgetProvider.SelfTestAsync());
}
else
{
    Log.Write("COM サーバーとして起動されていないので終了");
}
return 0;

internal static class Native
{
    public const uint CLSCTX_LOCAL_SERVER = 0x4;
    public const uint REGCLS_MULTIPLEUSE = 1;

    [DllImport("ole32.dll")]
    public static extern int CoRegisterClassObject(
        [MarshalAs(UnmanagedType.LPStruct)] Guid rclsid,
        [MarshalAs(UnmanagedType.IUnknown)] object pUnk,
        uint dwClsContext, uint flags, out uint lpdwRegister);

    [DllImport("ole32.dll")]
    public static extern int CoRevokeClassObject(uint dwRegister);
}
