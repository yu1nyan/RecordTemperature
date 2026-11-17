using System.Runtime.InteropServices;
using Microsoft.Windows.Widgets.Providers;
using WinRT;

namespace RecordTemperatureWidget;

// COM のクラスファクトリ。ウィジェットホストから頼まれたら WidgetProvider を作って渡す。
[ComImport]
[ComVisible(false)]
[InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
[Guid("00000001-0000-0000-C000-000000000046")]
internal interface IClassFactory
{
    [PreserveSig]
    int CreateInstance(IntPtr pUnkOuter, ref Guid riid, out IntPtr ppvObject);

    [PreserveSig]
    int LockServer(bool fLock);
}

[ComVisible(true)]
internal sealed class WidgetProviderFactory<T> : IClassFactory where T : IWidgetProvider, new()
{
    private const int CLASS_E_NOAGGREGATION = -2147221232;
    private const int E_NOINTERFACE = -2147467262;
    private static readonly Guid IID_IUnknown = new("00000000-0000-0000-C000-000000000046");

    public int CreateInstance(IntPtr pUnkOuter, ref Guid riid, out IntPtr ppvObject)
    {
        ppvObject = IntPtr.Zero;
        if (pUnkOuter != IntPtr.Zero)
        {
            return CLASS_E_NOAGGREGATION;
        }
        Log.Write($"CreateInstance riid={riid}");
        if (riid == typeof(T).GUID || riid == IID_IUnknown)
        {
            try
            {
                ppvObject = MarshalInspectable<IWidgetProvider>.FromManaged(new T());
                return 0;
            }
            catch (Exception e)
            {
                Log.Write($"WidgetProvider の作成に失敗: {e}");
                return Marshal.GetHRForException(e);
            }
        }
        return E_NOINTERFACE;
    }

    int IClassFactory.LockServer(bool fLock) => 0;
}
